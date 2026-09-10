"""Primary-bank review obligations; no learning, model calls or room delivery."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
import time
import uuid


class BankTransactionError(RuntimeError):
    def __init__(self, message, *, outcome, error_type=None):
        self.outcome = outcome
        self.error_type = error_type
        super().__init__(message)


class TransactionBoundaryError(BankTransactionError):
    pass


class AtomIdentityConflict(BankTransactionError):
    pass


class BankCommitUnknown(BankTransactionError):
    pass


def ensure_review_schema(conn):
    # execute, never executescript: executescript may commit a caller transaction.
    conn.execute('''CREATE TABLE IF NOT EXISTS atom_review_events (
        event_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
        review_id TEXT NOT NULL, event_kind TEXT NOT NULL CHECK(event_kind IN
          ('pending','projection_available','projection_unavailable','room_receipt_confirmed')),
        atom_id TEXT NOT NULL REFERENCES atoms(id), content_sha256 TEXT NOT NULL,
        scope TEXT NOT NULL, coordinate TEXT NOT NULL, ts INTEGER NOT NULL,
        payload TEXT NOT NULL)''')
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS atom_review_one_pending
        ON atom_review_events(atom_id,content_sha256) WHERE event_kind='pending' ''')
    conn.execute('''CREATE INDEX IF NOT EXISTS atom_review_by_review
        ON atom_review_events(review_id,ts,event_id)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS atom_review_schema_versions (
        version INTEGER PRIMARY KEY, applied_at_ns INTEGER NOT NULL,
        migration TEXT NOT NULL, journal_mode TEXT NOT NULL, synchronous INTEGER NOT NULL)''')
    if conn.execute('SELECT 1 FROM atom_review_schema_versions WHERE version != 1 LIMIT 1').fetchone():
        raise ValueError('unsupported bank review schema version')
    journal = conn.execute('PRAGMA journal_mode').fetchone()[0]
    synchronous = conn.execute('PRAGMA synchronous').fetchone()[0]
    conn.execute('''INSERT OR IGNORE INTO atom_review_schema_versions
        (version,applied_at_ns,migration,journal_mode,synchronous) VALUES (1,?,?,?,?)''',
        (time.time_ns(), 'atom_review_events_v1_no_backfill', journal, synchronous))
    receipt = conn.execute('SELECT migration FROM atom_review_schema_versions WHERE version=1').fetchone()
    if receipt is None or receipt[0] != 'atom_review_events_v1_no_backfill':
        raise ValueError('bank review schema receipt conflicts with migration')


def require_review_schema(conn):
    """Read-only schema availability gate shared by every recovery mutation."""
    rows = conn.execute('SELECT version,migration FROM atom_review_schema_versions').fetchall()
    if len(rows) != 1 or rows[0]['version'] != 1 or rows[0]['migration'] != 'atom_review_events_v1_no_backfill':
        raise ValueError('bank review schema receipt missing or unsupported')


def bank_atom_transaction(store, atom):
    """One owning transaction: exact atom insertion plus durable pending event."""
    digest = hashlib.sha256(atom.content.encode('utf-8')).hexdigest()
    review_id, event_id = uuid.uuid4().hex, uuid.uuid4().hex
    outcome = {'atom_id': atom.id, 'scope': atom.scope, 'coordinate': atom.coordinate,
               'content_sha256': digest, 'review_id': review_id, 'event_id': event_id,
               'insertion': 'unknown'}
    with store._lock:
        conn = store.conn
        if conn.in_transaction:
            raise TransactionBoundaryError('bank requires an owned transaction; existing transaction preserved',
                                           outcome={**outcome, 'insertion': 'not_attempted'})
        # Pin the durability contract on this connection, before BEGIN.
        try:
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('BEGIN IMMEDIATE')
            ensure_review_schema(conn)
            cursor = conn.execute('''INSERT OR IGNORE INTO atoms
                (id,coordinate,content,score,score_history,use_count,born_from,ts,scope,kind,valence,arousal)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (atom.id, atom.coordinate, atom.content, atom.score, atom.score_history,
                 atom.use_count, atom.born_from, atom.ts, atom.scope, atom.kind, atom.valence, atom.arousal))
            inserted = cursor.rowcount == 1
            actual = conn.execute('SELECT content,scope,coordinate FROM atoms WHERE id=?', (atom.id,)).fetchone()
            if actual is None or (actual['content'], actual['scope'], actual['coordinate']) != (
                    atom.content, atom.scope, atom.coordinate):
                raise AtomIdentityConflict('atom identity belongs to different content or placement',
                                           outcome={**outcome, 'insertion': 'conflict'})
            pending = conn.execute('''SELECT event_id,review_id,scope,coordinate FROM atom_review_events
                WHERE event_kind='pending' AND atom_id=? AND content_sha256=?''', (atom.id, digest)).fetchone()
            if pending and (pending['scope'], pending['coordinate']) != (atom.scope, atom.coordinate):
                raise AtomIdentityConflict('pending review identity conflicts with atom placement',
                                           outcome={**outcome, 'insertion': 'conflict'})
            if inserted and pending is None:
                conn.execute('''INSERT INTO atom_review_events
                    (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
                    VALUES (?,1,?,'pending',?,?,?,?,?,?)''',
                    (event_id, review_id, atom.id, digest, atom.scope, atom.coordinate, int(time.time()),
                     json.dumps({'origin': 'bank_insert', 'link_status': 'not_attempted'}, sort_keys=True)))
            elif pending is not None:
                outcome.update(review_id=pending['review_id'], event_id=pending['event_id'])
            else:
                # Historical row: no fabricated original obligation on no-op ingest.
                outcome.update(review_id=None, event_id=None)
            outcome['insertion'] = 'inserted' if inserted else 'unchanged'
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException as rollback_error:
                raise BankCommitUnknown('rollback unconfirmed; reopen and reconcile before retry',
                                        outcome={**outcome, 'insertion': 'unknown'},
                                        error_type=type(rollback_error).__name__) from None
            if isinstance(exc, BankTransactionError):
                raise
            if not isinstance(exc, Exception):
                raise
            raise BankTransactionError('bank transaction rolled back',
                                       outcome={**outcome, 'insertion': 'failed'},
                                       error_type=type(exc).__name__) from None
        try:
            conn.commit()
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException:
                pass
            raise BankCommitUnknown('bank commit unconfirmed; reopen and reconcile before retry',
                                    outcome={**outcome, 'insertion': 'unknown'},
                                    error_type=type(exc).__name__) from None
    return outcome


def review_event(store, event_id):
    """Read the persisted offer, never a regenerated graph or an earning fetch."""
    with store._lock:
        row = store.conn.execute(
            'SELECT rowid AS sequence,* FROM atom_review_events WHERE event_id=?',
            (event_id,)).fetchone()
        if row is None:
            raise KeyError('review event not found')
        result = dict(row)
        result['payload'] = json.loads(result['payload'])
        return result


def capture_review(store, review_id, *, link_status='unknown', fresh=False, references=None):
    """Persist a direct-edge offer; ordinary recovery reuses a successful capture.

    BEGIN IMMEDIATE holds a consistent graph snapshot through the event append.
    This operation never plants atoms, repairs links, or gives learning credit.
    """
    if link_status not in {'unknown', 'not_attempted', 'complete', 'complete_with_unlinked', 'partial'}:
        raise ValueError('unsupported link status')
    event_id = uuid.uuid4().hex
    outcome = {'review_id': review_id, 'event_id': event_id, 'insertion': 'unknown'}
    with store._lock:
        conn = store.conn
        if conn.in_transaction:
            raise TransactionBoundaryError('review capture requires an owned transaction',
                                           outcome={**outcome, 'insertion': 'not_attempted'})
        try:
            conn.execute('BEGIN IMMEDIATE')
            require_review_schema(conn)
            pending = conn.execute("SELECT * FROM atom_review_events WHERE review_id=? AND event_kind='pending'",
                                   (review_id,)).fetchone()
            if pending is None:
                raise KeyError('pending review not found')
            if not fresh:
                existing = conn.execute("SELECT event_id FROM atom_review_events WHERE review_id=? AND event_kind='projection_available' ORDER BY rowid DESC LIMIT 1",
                                        (review_id,)).fetchone()
                if existing:
                    state = review_state(store, review_id)
                    if state['integrity_status'] != 'valid':
                        raise BankTransactionError('stored review history is invalid; repair requires explicit diagnosis',
                                                   outcome={**outcome, 'insertion': 'not_attempted'})
                    conn.rollback()
                    return review_event(store, existing['event_id'])
            atom_id = pending['atom_id']
            outcome.update(atom_id=atom_id, content_sha256=pending['content_sha256'],
                           scope=pending['scope'], coordinate=pending['coordinate'])
            actual = conn.execute('SELECT content,scope,coordinate FROM atoms WHERE id=?', (atom_id,)).fetchone()
            if (actual is None or hashlib.sha256(actual['content'].encode('utf-8')).hexdigest() != pending['content_sha256']
                    or actual['scope'] != pending['scope'] or actual['coordinate'] != pending['coordinate']):
                raise AtomIdentityConflict('review atom identity mismatch', outcome=outcome)
            started = time.time_ns()
            try:
                rows = conn.execute("""SELECT e.from_id,e.to_id,e.relation,
                    CASE WHEN e.from_id=? THEN 'out' ELSE 'in' END AS direction,
                    CASE WHEN e.from_id=? THEN e.to_id ELSE e.from_id END AS neighbor_id,
                    a.id AS found_id,a.coordinate,a.scope,substr(a.content,1,240) AS preview
                    FROM atom_links e LEFT JOIN atoms a ON a.id=
                      CASE WHEN e.from_id=? THEN e.to_id ELSE e.from_id END
                    WHERE (e.from_id=? OR e.to_id=?) AND e.superseded_on=0
                    ORDER BY e.from_id,e.to_id,e.relation""", (atom_id,)*5).fetchall()
                edges = []
                for row in rows:
                    edge = dict(row)
                    edge['missing_neighbor'] = edge.pop('found_id') is None
                    edges.append(edge)
                kind = 'projection_available'
                payload = {'edges': edges, 'total': len(edges), 'link_status': link_status,
                           'snapshot_started_ns': started, 'snapshot_finished_ns': time.time_ns(),
                           'capture_origin': 'fresh' if fresh else 'capture_or_resume',
                           'learning_credit': False, 'references': references}
            except Exception as exc:
                kind = 'projection_unavailable'
                payload = {'error_type': type(exc).__name__, 'link_status': link_status, 'references': references,
                           'snapshot_started_ns': started, 'snapshot_finished_ns': time.time_ns()}
            conn.execute("""INSERT INTO atom_review_events
                (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
                VALUES (?,1,?,?,?,?,?,?,?,?)""",
                (event_id, review_id, kind, atom_id, pending['content_sha256'], pending['scope'],
                 pending['coordinate'], int(time.time()), json.dumps(payload, sort_keys=True)))
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException as rollback_error:
                raise BankCommitUnknown('review rollback unconfirmed; reconcile before retry',
                                        outcome=outcome, error_type=type(rollback_error).__name__) from None
            if isinstance(exc, BankTransactionError) or not isinstance(exc, Exception):
                raise
            raise BankTransactionError('review capture rolled back; pending obligation retained',
                                       outcome={**outcome, 'insertion': 'failed'},
                                       error_type=type(exc).__name__) from None
        try:
            conn.commit()
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException:
                pass
            raise BankCommitUnknown('review capture commit unconfirmed; reconcile exact event before retry',
                                    outcome=outcome, error_type=type(exc).__name__) from None
        return review_event(store, event_id)


def reconcile_bank_commit(db_path, outcome):
    """Observe exact durable atom/event identities through a new read-only connection.

    Missing/unreadable databases stay unknown and are never created. This reports
    durable presence, not whether this invocation originally inserted the atom.
    It never authorizes automatic mutation retries.
    """
    required = ('atom_id', 'content_sha256', 'scope', 'coordinate', 'review_id', 'event_id')
    if any(key not in outcome for key in required):
        raise ValueError('reconciliation requires the complete immutable bank identity')
    result = {key: outcome[key] for key in required}
    result.update(status='unknown', retry_safe=False)
    conn = None
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN')
        atom = conn.execute('SELECT content,scope,coordinate FROM atoms WHERE id=?',
                            (outcome['atom_id'],)).fetchone()
        schema_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='atom_review_events'").fetchone()
        event = (conn.execute('SELECT * FROM atom_review_events WHERE event_id=?',
                              (outcome['event_id'],)).fetchone()
                 if schema_exists and outcome['event_id'] else None)
        if atom is None and event is None:
            result['status'] = 'absent'
        elif atom is None:
            result['status'] = 'inconsistent'
        else:
            exact_atom = (hashlib.sha256(atom['content'].encode('utf-8')).hexdigest() == outcome['content_sha256']
                          and atom['scope'] == outcome['scope'] and atom['coordinate'] == outcome['coordinate'])
            if not exact_atom:
                result['status'] = 'inconsistent'
            elif outcome['review_id'] is None and outcome['event_id'] is None:
                result['status'] = 'present_historical_unqueued'
            elif event is None:
                result['status'] = 'inconsistent'
            elif event['event_kind'] == 'pending' and all(event[key] == outcome[key] for key in required):
                result['status'] = 'present'
            else:
                result['status'] = 'inconsistent'
    except Exception as exc:
        result.update(status='unknown', error_type=type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
    return result


def review_state(store, review_id, *, room=None):
    """Fold durable sequence, keeping capture and destination delivery separate."""
    with store._lock:
        rows = store.conn.execute(
            'SELECT rowid AS sequence,* FROM atom_review_events WHERE review_id=? ORDER BY rowid',
            (review_id,)).fetchall()
    if not rows:
        raise KeyError('review not found')
    pending = None
    available = None
    latest_notice = None
    sources = {}
    confirmed = set()
    diagnostics = []
    identity_fields = ('atom_id', 'content_sha256', 'scope', 'coordinate')
    for row in rows:
        event = dict(row)
        try:
            payload = json.loads(event['payload'])
            if not isinstance(payload, dict):
                raise ValueError('event payload must be an object')
        except (ValueError, TypeError):
            diagnostics.append({'event_id': event['event_id'], 'error': 'invalid_payload'})
            continue
        if event['schema_version'] != 1:
            diagnostics.append({'event_id': event['event_id'], 'error': 'unsupported_schema'})
            continue
        kind = event['event_kind']
        if kind == 'projection_available':
            edges = payload.get('edges')
            total = payload.get('total')
            valid_edges = isinstance(edges, list) and all(
                isinstance(edge, dict) and all(isinstance(edge.get(key), str) and edge[key]
                    for key in ('from_id', 'to_id', 'relation', 'neighbor_id'))
                and edge.get('direction') in ('in', 'out')
                and type(edge.get('missing_neighbor')) is bool for edge in edges)
            if not valid_edges or type(total) is not int or total != len(edges):
                diagnostics.append({'event_id': event['event_id'], 'error': 'invalid_projection_payload'})
                continue
        if kind == 'pending':
            if pending is not None:
                diagnostics.append({'event_id': event['event_id'], 'error': 'duplicate_pending'})
                continue
            pending = event
        if pending is None or any(event[k] != pending[k] for k in identity_fields):
            diagnostics.append({'event_id': event['event_id'], 'error': 'identity_mismatch'})
            continue
        if kind in {'pending', 'projection_available', 'projection_unavailable'}:
            sources[event['event_id']] = event
            latest_notice = event
            if kind == 'projection_available':
                available = event
        elif kind == 'room_receipt_confirmed':
            source_id, destination = payload.get('source_event_id'), payload.get('room')
            if not isinstance(source_id, str) or source_id not in sources or not isinstance(destination, str) or not destination:
                diagnostics.append({'event_id': event['event_id'], 'error': 'invalid_confirmation_reference'})
                continue
            confirmed.add((source_id, destination))
        else:
            diagnostics.append({'event_id': event['event_id'], 'error': 'unknown_event_kind'})
    source = available or latest_notice
    capture = ('available' if available else 'unavailable' if latest_notice and
               latest_notice['event_kind'] == 'projection_unavailable' else 'pending')
    invalid = bool(diagnostics) or pending is None
    # Destinations are a projection of *validated* confirmations for the final
    # selected source only.  They are historical facts, never a routing hint.
    confirmed_destinations = () if invalid or source is None else tuple(sorted({
        destination for source_id, destination in confirmed if source_id == source['event_id']
    }))
    return {'review_id': review_id, 'capture_status': 'unknown' if invalid else capture,
            'capture_required': None if invalid else available is None, 'source_event_id': source['event_id'] if source else None,
            'scope': pending['scope'] if pending is not None and not invalid else None,
            'pending_sequence': pending['sequence'] if pending is not None and not invalid else None,
            'confirmed_destinations': confirmed_destinations,
            'room': room, 'delivery_status': ('unknown' if invalid else 'not_requested' if room is None else
                'confirmed' if source and (source['event_id'], room) in confirmed else 'unconfirmed'),
            'integrity_status': 'invalid' if invalid else 'valid',
            'diagnostics': diagnostics, 'last_sequence': rows[-1]['sequence']}


def recovery_eligibility(store, review_id):
    """Read one folded review before any routing or delivery decision.

    Scope and cursor sequence come only from the persisted pending event.  An
    invalid row may expose that historical scope for reporting, never routing.
    """
    try:
        state = review_state(store, review_id)
    except KeyError:
        return {'review_id': review_id, 'scope': None, 'sequence': None,
                'integrity_status': 'invalid', 'capture_required': None,
                'source_event_id': None, 'confirmed_destinations': (),
                'status': 'invalid', 'diagnostics': [{'error': 'review_not_found'}]}
    status = ('invalid' if state['integrity_status'] != 'valid' else
              'needs_route' if state['capture_required'] or not state['confirmed_destinations'] else
              'complete')
    return {'review_id': review_id, 'scope': state['scope'], 'sequence': state['pending_sequence'],
            'integrity_status': state['integrity_status'], 'capture_required': state['capture_required'],
            'source_event_id': state['source_event_id'],
            'confirmed_destinations': state['confirmed_destinations'], 'status': status,
            'diagnostics': state['diagnostics']}


def pending_recovery_page(store, *, limit=100, after_sequence=0):
    """Globally page pending review IDs, folding each selected row exactly once.

    The raw pending sequence remains the cursor.  Duplicate pending rows in a
    page intentionally collapse to one invalid eligibility record.
    """
    if type(limit) is not int or limit < 1 or type(after_sequence) is not int or after_sequence < 0:
        raise ValueError('recovery limit and cursor must be nonnegative integers with positive limit')
    with store._lock:
        require_review_schema(store.conn)
        rows = store.conn.execute("SELECT rowid AS sequence,review_id,scope FROM atom_review_events "
                                  "WHERE event_kind='pending' AND rowid>? ORDER BY rowid LIMIT ?",
                                  (after_sequence, limit + 1)).fetchall()
    has_more = len(rows) > limit
    page = rows[:limit]
    seen, eligibility = set(), []
    for row in page:
        if row['review_id'] in seen:
            continue
        seen.add(row['review_id'])
        item = recovery_eligibility(store, row['review_id'])
        if item['status'] == 'invalid':
            # Page provenance is diagnostic only; invalid history never routes.
            item = {**item, 'scope': row['scope'], 'sequence': row['sequence'],
                    'scope_origin': 'page_row_not_validated'}
        eligibility.append(item)
    return {'ok': not has_more and all(item['status'] != 'invalid' for item in eligibility),
            'outcomes': eligibility, 'scanned': len(page), 'remaining': int(has_more),
            'remaining_is_lower_bound': has_more,
            'next_sequence': page[-1]['sequence'] if has_more and page else 0}


def _main(argv=None):
    """Native recovery/retrieval door; existing banks only, no earning reads."""
    import argparse
    import threading
    from types import SimpleNamespace
    from .cards import DEFAULT_V2_DB
    parser = argparse.ArgumentParser(prog='echelon bank-review')
    parser.add_argument('--bank', default=str(DEFAULT_V2_DB))
    commands = parser.add_subparsers(dest='action', required=True)
    show = commands.add_parser('show', help='retrieve an immutable persisted offer')
    show.add_argument('event_id')
    show.add_argument('--limit', type=int, default=20)
    show.add_argument('--full', action='store_true')
    state = commands.add_parser('state', help='inspect capture and delivery independently')
    state.add_argument('review_id')
    state.add_argument('--room')
    resume = commands.add_parser('resume', help='capture neighbors only; never replant or repair links')
    resume.add_argument('review_id')
    deliver = commands.add_parser('deliver', help='deliver an exact persisted event to a room')
    deliver.add_argument('event_id')
    deliver.add_argument('--room', required=True)
    recover = commands.add_parser('recover', help='resume unfinished reviews for a scope')
    recover.add_argument('--scope', required=True)
    recover.add_argument('--room', required=True)
    recover.add_argument('--limit', type=int, default=100)
    recover.add_argument('--after-sequence', type=int, default=0)
    args = parser.parse_args(argv)
    if args.action == 'show' and args.limit < 0:
        parser.error('--limit must be nonnegative')
    conn = None
    try:
        mode = 'rw' if args.action in {'resume', 'deliver', 'recover'} else 'ro'
        conn = sqlite3.connect(Path(args.bank).resolve().as_uri() + '?mode=' + mode, uri=True)
        conn.row_factory = sqlite3.Row
        store = SimpleNamespace(conn=conn, _lock=threading.RLock())
        if args.action == 'recover':
            conn.execute('PRAGMA synchronous=FULL')
            result = recover_reviews(store, args.scope, args.room, limit=args.limit, after_sequence=args.after_sequence)
            ok = result['ok']
        elif args.action == 'deliver':
            conn.execute('PRAGMA synchronous=FULL')
            result = deliver_review(store, args.event_id, args.room)
            ok = result['ok']
        elif args.action == 'state':
            from echelon_engine import workcycle
            destination = str(workcycle.room_dir(args.room)) if args.room else None
            result = review_state(store, args.review_id, room=destination)
            ok = result['integrity_status'] == 'valid'
        else:
            if args.action == 'resume':
                conn.execute('PRAGMA synchronous=FULL')
                result = capture_review(store, args.review_id)
            else:
                result = review_event(store, args.event_id)
            state = review_state(store, result['review_id'])
            ok = result['event_kind'] != 'projection_unavailable' and state['integrity_status'] == 'valid'
            if state['integrity_status'] != 'valid':
                result['integrity'] = state
            if ok and args.action == 'show' and not args.full and 'edges' in result['payload']:
                edges = result['payload']['edges']
                result['payload']['edges'] = edges[:args.limit]
                result['display'] = {'shown': min(len(edges), args.limit), 'total': len(edges),
                                     'omitted': max(0, len(edges)-args.limit),
                                     'full_result': {'verb': 'bank-review', 'bank': str(Path(args.bank).resolve()),
                                                     'action': 'show', 'event_id': args.event_id, 'full': True}}
        print(json.dumps({'ok': ok, 'result': result}, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1
    except Exception as exc:
        failure = {'ok': False, 'error_type': type(exc).__name__, 'retry_safe': False}
        if isinstance(exc, BankTransactionError):
            failure['outcome'] = exc.outcome
        print(json.dumps(failure, sort_keys=True))
        return 1
    finally:
        if conn is not None:
            conn.close()


def deliver_review(store, event_id, room):
    """Deliver one immutable event; confirm only the exact durable room receipt."""
    from echelon_engine import workcycle
    with store._lock:
        if store.conn.in_transaction:
            raise TransactionBoundaryError('delivery requires committed bank state',
                                           outcome={'event_id': event_id, 'insertion': 'not_attempted'})
    with store._lock:
        require_review_schema(store.conn)
    destination = str(workcycle.room_dir(room))
    event = review_event(store, event_id)
    if event['event_kind'] not in {'pending', 'projection_available', 'projection_unavailable'}:
        raise ValueError('only review notices can be delivered')
    state = review_state(store, event['review_id'])
    if state['integrity_status'] != 'valid':
        raise ValueError('invalid review history cannot be delivered')
    payload_digest = hashlib.sha256(json.dumps(event['payload'], sort_keys=True,
                                              ensure_ascii=False).encode('utf-8')).hexdigest()
    text = (f"Bank review {event['review_id']}: {event['event_kind']}; "
            f"atom={event['atom_id']}; event={event_id}; payload_sha256={payload_digest}. "
            f"Retrieve with echelon bank-review show {event_id} --full")
    receipt = workcycle.receipt(Path(destination), 'route', text, origin='bank-review',
                               ref='bank-review:' + event_id, strict_identity=True, fold_now=False)
    if receipt.get('ok') is not True:
        return {'ok': False, 'event_id': event_id, 'room': destination,
                'delivery_status': 'unconfirmed', 'error_type': receipt.get('error_type', 'RoomReceiptFailure')}
    confirmation_id = hashlib.sha256((event_id + '\0' + destination).encode('utf-8')).hexdigest()
    outcome = {key: event[key] for key in ('atom_id', 'content_sha256', 'scope', 'coordinate', 'review_id')}
    outcome.update(event_id=confirmation_id, insertion='unknown')
    with store._lock:
        conn = store.conn
        if conn.in_transaction:
            raise TransactionBoundaryError('confirmation requires an owned transaction', outcome=outcome)
        try:
            conn.execute('BEGIN IMMEDIATE')
            confirmation = {'source_event_id': event_id, 'room': destination,
                            'receipt_identity_sha256': receipt['receipt']['identity_sha256']}
            encoded = json.dumps(confirmation, sort_keys=True)
            conn.execute("""INSERT OR IGNORE INTO atom_review_events
                (event_id,schema_version,review_id,event_kind,atom_id,content_sha256,scope,coordinate,ts,payload)
                VALUES (?,1,?,'room_receipt_confirmed',?,?,?,?,?,?)""",
                (confirmation_id, event['review_id'], event['atom_id'], event['content_sha256'],
                 event['scope'], event['coordinate'], int(time.time()), encoded))
            actual = conn.execute('SELECT * FROM atom_review_events WHERE event_id=?', (confirmation_id,)).fetchone()
            if (actual['payload'] != encoded or actual['event_kind'] != 'room_receipt_confirmed'
                    or any(actual[k] != outcome[k] for k in ('atom_id', 'content_sha256', 'scope', 'coordinate', 'review_id'))):
                raise ValueError('confirmation identity conflict')
        except BaseException:
            conn.rollback()
            raise
        try:
            conn.commit()
        except BaseException as exc:
            try:
                conn.rollback()
            except BaseException:
                pass
            raise BankCommitUnknown('room received event; bank confirmation unconfirmed', outcome=outcome,
                                    error_type=type(exc).__name__) from None
    return {'ok': True, 'event_id': event_id, 'room': destination,
            'confirmation_event_id': confirmation_id, 'delivery_status': 'confirmed'}


def recover_review(store, review_id, room):
    """Recover one eligible review to one exact destination.

    This is the pulse-safe door: it never scans a scope and it skips only a
    confirmation for the current source and the exact supplied destination.
    """
    from echelon_engine import workcycle
    destination = str(workcycle.room_dir(room))
    state = review_state(store, review_id, room=destination)
    if state['integrity_status'] != 'valid':
        return {'review_id': review_id, 'ok': False, 'attempted': False,
                'error_type': 'ReviewIntegrityError'}
    if not state['capture_required'] and state['delivery_status'] == 'confirmed':
        return {'review_id': review_id, 'ok': True, 'attempted': False,
                'delivery_status': 'confirmed', 'skipped_exact_destination': True}
    if state['capture_required']:
        event = capture_review(store, review_id)
        source_id = event['event_id']
    else:
        source_id = state['source_event_id']
    result = deliver_review(store, source_id, destination)
    refreshed = review_state(store, review_id, room=destination)
    return {**result, 'review_id': review_id, 'attempted': True,
            'capture_status': refreshed['capture_status'],
            'ok': result['ok'] and refreshed['capture_status'] == 'available'}


def recover_reviews(store, scope, room, *, limit=100, after_sequence=0):
    """Operator scope door: deliver every review in ``scope`` to this room.

    This explicit operator command retains its historical re-deliver-to-passed-
    room meaning.  Pulse must use :func:`recover_review` after eligibility and
    unique routing, never this scope-wide scan.
    """
    from echelon_engine import workcycle
    if type(limit) is not int or limit < 1 or type(after_sequence) is not int or after_sequence < 0:
        raise ValueError('recovery limit and cursor must be nonnegative integers with positive limit')
    destination = str(workcycle.room_dir(room))
    with store._lock:
        require_review_schema(store.conn)
        rows = store.conn.execute("SELECT rowid AS sequence,review_id FROM atom_review_events WHERE event_kind='pending' AND scope=? AND rowid>? ORDER BY rowid LIMIT ?",
                                  (scope, after_sequence, limit + 1)).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    outcomes = []
    remaining = int(has_more)
    for row in rows:
        review_id = row['review_id']
        try:
            state = review_state(store, review_id, room=destination)
            if state['integrity_status'] != 'valid':
                outcomes.append({'review_id': review_id, 'ok': False, 'error_type': 'ReviewIntegrityError'})
                continue
            if not state['capture_required'] and state['delivery_status'] == 'confirmed':
                continue
            outcomes.append(recover_review(store, review_id, destination))
        except Exception as exc:
            outcomes.append({'review_id': review_id, 'attempted': True, 'ok': False,
                             'error_type': type(exc).__name__})
    return {'ok': remaining == 0 and all(o['ok'] for o in outcomes), 'scope': scope,
            'room': destination, 'outcomes': outcomes, 'remaining': remaining,
            'remaining_is_lower_bound': has_more, 'scanned': len(rows),
            'next_sequence': rows[-1]['sequence'] if has_more and rows else 0}


def validated_rooms(entries):
    """Fold a room registry into (destinations, errors, roots) — the ONE builder.

    Both the CC pulse and Board health must derive routing inputs from this single
    function. Two hand-written loops previously agreed on every registration the live
    registry happens to contain, but disagreed on shapes it does not (a non-str path
    was validated by one and rejected by the other), so the pulse could deliver a
    scope that health still reported unrouted and the heartbeat could never clear.
    A shared resolver is not enough; the builder has to be shared too.

    ``roots`` maps a scope to its CANONICAL ROOT room: the registration whose NAME is
    exactly the scope name (owner rule, 2026-09-09).
    """
    destinations, errors, roots = {}, {}, {}
    for name, entry in (entries or {}).items():
        scope = entry.get('scope') if isinstance(entry, dict) else None
        raw_path = entry.get('path') if isinstance(entry, dict) else None
        if (not isinstance(scope, str) or not scope
                or not isinstance(raw_path, str) or not raw_path):
            errors.setdefault(scope if isinstance(scope, str) else '', []).append({
                'room': str(raw_path), 'error_type': 'ValueError',
                'status': 'registry_entry_missing_scope_or_path'})
            continue
        try:
            destination = Path(raw_path).resolve()
            record = json.loads((destination / 'room.json').read_text(encoding='utf-8'))
            if not isinstance(record, dict) or record.get('scope') != scope:
                raise ValueError('routing_scope_mismatch')
            destinations.setdefault(scope, set()).add(str(destination))
            if name == scope:
                if scope in roots and roots[scope] != str(destination):
                    errors.setdefault(scope, []).append({
                        'room': str(raw_path), 'error_type': 'ValueError',
                        'status': 'routing_ambiguous_root'})
                    continue
                roots[scope] = str(destination)
        except Exception as exc:
            errors.setdefault(scope, []).append({
                'room': str(raw_path), 'error_type': type(exc).__name__,
                'status': 'routing_scope_mismatch'
                          if isinstance(exc, ValueError) and str(exc) == 'routing_scope_mismatch'
                          else 'routing_invalid'})
    return destinations, errors, roots


def resolve_destination(destinations, scope, roots=None):
    """Return the one room a scope delivers to, or None when it stays fail-closed.

    One registered room routes. Several rooms route to the scope's CANONICAL ROOT
    (owner rule, 2026-09-09) when ``roots`` names one for that scope and it is among
    the validated rooms; otherwise the scope stays unrouted. ``roots=None`` keeps the
    strict pre-rule behaviour, so callers that cannot supply roots do not silently
    start routing. Health and recovery must both go through this one resolver, or a
    routed review would still be counted unrouted by the other surface.
    """
    choices = _destination_set(destinations, scope)
    if len(choices) == 1:
        return next(iter(choices))
    # Everything below only ever REFUSES to route. A malformed ``roots`` must not
    # raise: this runs inside review_health's per-review loop, whose blanket except
    # would turn an honest `degraded` read into `unavailable` and hide real state.
    # Failing closed is both the safe answer and the truthful one.
    if not choices or not isinstance(roots, dict) or not isinstance(scope, str):
        return None
    root = roots.get(scope)
    if not isinstance(root, str) or not root:
        return None
    try:
        root = str(Path(root).resolve())
    except (OSError, ValueError):
        return None
    return root if root in choices else None


def review_health(db_path, *, destinations=None, roots=None, budget_seconds=2.0):
    """Non-earning read-only health; unavailable schema is never an empty healthy bank."""
    import threading
    import time
    import math
    from types import SimpleNamespace
    if type(budget_seconds) not in (int, float) or not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("health budget must be a positive finite number")
    deadline = time.monotonic() + budget_seconds
    conn = None
    result = {'ok': False, 'status': 'unavailable', 'reviews': 0,
              'pending_capture': 0, 'invalid': 0, 'available': 0, 'unconfirmed_delivery': 0, 'unrouted': 0,
              'completed': 0, 'completed_with_absent_destination': 0,
              'destination_presence': {'present': 0, 'absent': 0, 'unknown': 0},
              'assessment_complete': False, 'counts_are_partial': True}
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        conn.execute('BEGIN')
        store = SimpleNamespace(conn=conn, _lock=threading.RLock())
        versions = conn.execute('SELECT version,migration FROM atom_review_schema_versions').fetchall()
        if len(versions) != 1 or versions[0]['version'] != 1 or versions[0]['migration'] != 'atom_review_events_v1_no_backfill':
            raise ValueError('bank review schema receipt missing or unsupported')
        rows = conn.execute('SELECT DISTINCT review_id FROM atom_review_events')
        for row in rows:
            if time.monotonic() >= deadline:
                raise TimeoutError('bank health assessment budget exhausted')
            result['reviews'] += 1
            eligibility = recovery_eligibility(store, row['review_id'])
            if eligibility['status'] == 'invalid':
                result['invalid'] += 1
                continue
            if eligibility['status'] == 'complete':
                result['available'] += 1
                result['completed'] += 1
                presence = delivery_presence(eligibility, destinations)
                result['destination_presence'][presence['presence']] += 1
                if presence['absent_count']:
                    result['completed_with_absent_destination'] += 1
                continue
            if eligibility['capture_required']:
                result['pending_capture'] += 1
            else:
                result['available'] += 1
            if destinations is not None:
                if resolve_destination(destinations, eligibility['scope'], roots) is None:
                    result['unrouted'] += 1
                else:
                    result['unconfirmed_delivery'] += 1
        if time.monotonic() >= deadline:
            raise TimeoutError('bank health assessment budget exhausted')
        result['status'] = 'degraded' if result['invalid'] or result['unrouted'] else 'backlogged' if result['pending_capture'] or result['unconfirmed_delivery'] else 'ok'
        result['ok'] = result['status'] == 'ok'
        result.update(assessment_complete=True, counts_are_partial=False)
        result['delivery_assessment'] = 'requires_destination' if destinations is None else 'registered_destinations'
    except Exception as exc:
        result.update(ok=False, status='unavailable', error_type=type(exc).__name__)
    finally:
        if conn is not None:
            conn.close()
    return result


def _destination_set(destinations, scope):
    """Normalize a scope's current registered room set for presence comparison."""
    if not isinstance(destinations, dict):
        raise ValueError('destinations must be a scope-to-room-set mapping')
    values = destinations.get(scope, set())
    # Retain a string compatibility shim for existing direct callers; CC passes
    # a set so ambiguity cannot be silently collapsed.
    if isinstance(values, str):
        values = {values}
    if not isinstance(values, (set, frozenset, list, tuple)):
        raise ValueError('destination scope value must be a room set')
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError('destination room must be a nonempty string')
    return {str(Path(value).resolve()) for value in values}


def delivery_presence(eligibility, destinations=None):
    """Annotate a complete review's historical destinations without routing.

    ``destinations`` is a full scope-to-current-room-set mapping.  The returned
    value intentionally distinguishes no mapping (unknown) from an explicitly
    supplied scope that no longer contains a recorded destination (absent).
    """
    if not isinstance(eligibility, dict) or eligibility.get('status') != 'complete':
        return {'presence': 'not_applicable', 'destinations': (), 'absent_count': 0}
    recorded = eligibility.get('confirmed_destinations')
    if not isinstance(recorded, tuple) or not all(isinstance(room, str) and room for room in recorded):
        raise ValueError('complete eligibility has invalid confirmed destinations')
    current = None if destinations is None else _destination_set(destinations, eligibility.get('scope'))
    annotated = tuple({'room': room, 'presence': 'unknown' if current is None else
                       'present' if str(Path(room).resolve()) in current else 'absent'}
                      for room in recorded)
    absent_count = sum(item['presence'] == 'absent' for item in annotated)
    overall = 'unknown' if current is None else 'absent' if absent_count else 'present'
    return {'presence': overall, 'destinations': annotated, 'absent_count': absent_count}
