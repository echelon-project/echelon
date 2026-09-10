#!/usr/bin/env python3
"""Reusable multilingual evidence-graph extraction library (Python 3.10+).

Keep code_extract.py and lang_core.json in the same directory.
  python code_extract.py --root /repo --language "php,html,css,javascript,python" --out graph.json
  python code_extract.py --list-languages
  python code_extract.py --self-test
  python code_extract.py query graph.json find create_order
  python code_extract.py compare ours.json peer.json --min-peers 1 --out deltas.json

Library API:
  from code_extract import extract_repository, load_language_core, query_graph
  graph = extract_repository('/repo', languages='php,html,javascript,python')
  # registry_path=...; capabilities={'orders.php:create': 'order.create'}

Architecture: ONE engine .py + ONE editable language registry .json.
Native Python AST and HTML adapters retain grammar-aware information. The
regex_balanced adapter reads declarations, observations and references from
JSON patterns, masks comments/strings, and matches configured delimiters.
New languages fitting this model need only another JSON profile. A new grammar
or semantic analysis beyond those primitives requires a new engine adapter.

Evidence is static, not a proof of runtime behavior or vulnerability. Generic
calls remain unresolved: matching a name is not binding resolution. PHP/JS/CSS
coverage is intentionally labeled partial. Inline script/style regions are
scanned only when both the host and embedded language are selected. Source
positions refer to original files; Python columns count UTF-8 bytes, regex and
HTML columns count Unicode code points. No target code is imported/executed.

Comparisons default to explicitly mapped capabilities; inferred labels are
opt-in and do not establish semantic equivalence. Registry/rule mismatches are
excluded. Mixed-language votes need human review of extraction coverage.
Only observed features vote; absence is not proof of missing protection.
JSON is the exchange schema; no .des grammar was supplied.

The prior Echelon atlas/JS legacy adapters are not dependencies of this library.
The rich Python extractor is carried forward from differential_graph.py.
"""
from __future__ import annotations
import argparse
import ast
import bisect
import collections
import fnmatch
import hashlib
import json
import os
import re
import sys
import tokenize
from pathlib import Path
from html.parser import HTMLParser

SCHEMA = 'python-evidence-graph/1.0'
DEFAULT_EXCLUDES = ('.git', '.venv', 'venv', '__pycache__', 'node_modules',
                    'site-packages', 'build', 'dist', '*.egg-info')
DEFAULT_RULES = {
    'authorization_candidate': ['*authorize*', '*permission*', '*check_access*'],
    'authentication_candidate': ['*authenticate*', '*login_required*'],
    'validation_candidate': ['*validate*', '*full_clean*', '*verify_signature*'],
    'database_read_candidate': ['*.query', '*.filter', '*.select', '*.get'],
    'database_write_candidate': ['*.save', '*.insert', '*.update', '*.delete', '*.execute'],
    'transaction_candidate': ['*.atomic', '*.transaction', '*.commit', '*.rollback'],
    'network_candidate': ['requests.*', 'httpx.*', 'urllib.request.*', '*.fetch'],
    'filesystem_candidate': ['open', 'builtins.open', '*.read_text', '*.write_text', '*.unlink'],
    'process_candidate': ['subprocess.*', 'os.system', 'os.popen'],
    'dynamic_execution_candidate': ['eval', 'exec', 'builtins.eval', 'builtins.exec'],
    'deserialization_candidate': ['pickle.*', 'yaml.load', 'marshal.loads'],
    'logging_candidate': ['logging.*', '*.debug', '*.info', '*.warning', '*.error'],
}




def _dotted(rel_path: Path) -> str:
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def _expr(node):
    return ast.unparse(node) if node is not None else None


def _literal(node):
    try:
        value = ast.literal_eval(node)
        json.dumps(value)
        return {'known': True, 'value': value}
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return {'known': False, 'expression': _expr(node)}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _name(node.value)
        return parent + '.' + node.attr if parent else ''
    return ''


def _owned_walk(node):
    """Walk one executable scope, never attributing nested bodies to its parent."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _owned_walk(child)


def discover_python(root, packages=None, excludes=DEFAULT_EXCLUDES, extensions=(".py",)):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Not a repository directory: {root}')
    found = set()
    for package in packages or ['.']:
        base = (root / package).resolve()
        if not base.is_relative_to(root):
            raise ValueError(f'Package escapes root: {package}')
        if not base.exists():
            raise ValueError(f'Package does not exist: {package}')
        candidates = [base] if base.is_file() else None
        if candidates is None:
            candidates = []
            for directory, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not any(fnmatch.fnmatch(d, p) for p in excludes)
                                  and not Path(directory, d).is_symlink())
                candidates.extend(Path(directory, f) for f in sorted(files) if any(f.lower().endswith(e.lower()) for e in extensions))
        for path in candidates:
            if any(path.name.lower().endswith(e.lower()) for e in extensions) and not path.is_symlink() and path.resolve().is_relative_to(root):
                found.add(path)
    return sorted(found)


class PythonGraphExtractor:
    """Two-pass static extractor. Instances are single-use; use extract_python()."""
    def __init__(self, root, packages=None, capabilities=None, rules=None,
                 excludes=DEFAULT_EXCLUDES, revision=None, source_url=None, extensions=(".py",)):
        self.root = Path(root).resolve()
        self.extensions = extensions
        self.packages = packages or ['.']
        self.capabilities = capabilities or {}
        self.rules = rules if rules is not None else DEFAULT_RULES
        if not isinstance(self.capabilities, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                             for k, v in self.capabilities.items()):
            raise ValueError('Capabilities must map symbol keys to strings')
        if not isinstance(self.rules, dict) or not all(isinstance(k, str) and isinstance(v, list)
                and all(isinstance(p, str) for p in v) for k, v in self.rules.items()):
            raise ValueError('Rules must map tag names to lists of glob patterns')
        self.excludes, self.revision, self.source_url = excludes, revision, source_url
        self.nodes, self.edges, self.sources, self.diagnostics = {}, [], {}, []
        self.scopes, self.ast_ids, self.symbols, self.modules = {}, {}, {}, {}
        self.units, self.pending = [], []
        self.add_node('repository:.', 'repository', label='repository')

    def evidence(self, path, node):
        return {'path': path, 'line': getattr(node, 'lineno', 1),
                'column': getattr(node, 'col_offset', 0),
                'end_line': getattr(node, 'end_lineno', getattr(node, 'lineno', 1)),
                'end_column': getattr(node, 'end_col_offset', 0),
                'source_sha256': self.sources[path]['sha256']}

    def add_node(self, nid, node_type, **fields):
        if nid not in self.nodes:
            self.nodes[nid] = {'id': nid, 'type': node_type, **fields}
        return nid

    def edge(self, source, target, rel, evidence=None, confidence='observed', **fields):
        self.edges.append({'from': source, 'to': target, 'rel': rel,
                           'confidence': confidence, **({'evidence': evidence} if evidence else {}), **fields})

    def symbol(self, full, nid):
        self.symbols.setdefault(full, []).append(nid)

    def index_scope(self, body, sid, path, dotted, qual='', class_id=None):
        scope = self.scopes[sid]
        # Statements nested in branches still bind names in this Python scope.
        def statements(items):
            for item in items:
                yield item
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    for field in ('body', 'orelse', 'finalbody'):
                        value = getattr(item, field, None)
                        if isinstance(value, list):
                            yield from statements(value)
                    for handler in getattr(item, 'handlers', []):
                        yield from statements(handler.body)
                    for case in getattr(item, 'cases', []):
                        yield from statements(case.body)
        for node in statements(body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = qual + '.' + node.name if qual else node.name
                base = f'symbol:{path}:{q}'
                nid = base if base not in self.nodes else base + f'@{node.lineno}'
                kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
                ev = self.evidence(path, node)
                self.add_node(nid, kind, label=node.name, qualname=q, path=path, evidence=ev,
                              docstring=ast.get_docstring(node), decorators=[_expr(d) for d in node.decorator_list],
                              visibility='private' if node.name.startswith('_') else 'public',
                              **({'async': isinstance(node, ast.AsyncFunctionDef)} if kind == 'function' else {}))
                self.edge(sid, nid, 'contains', ev)
                self.ast_ids[id(node)] = nid
                scope['bindings'].setdefault(node.name, []).append(nid)
                self.symbol((dotted + '.' + q).strip('.'), nid)
                self.scopes[nid] = {'parent': sid, 'bindings': {}, 'imports': {}, 'locals': set(),
                                    'class_id': class_id, 'kind': kind, 'path': path, 'dotted': dotted}
                self.index_scope(node.body, nid, path, dotted, q, nid if kind == 'class' else class_id)
                if kind == 'function':
                    self.contract(node, nid, path)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module_parts = dotted.split('.') if path.endswith('__init__.py') else dotted.split('.')[:-1]
                if isinstance(node, ast.ImportFrom):
                    if node.level > len(module_parts) and node.level:
                        self.diagnostics.append({'kind': 'relative_import_outside_package', 'evidence': self.evidence(path, node)})
                    base = '.'.join(module_parts[:len(module_parts) - node.level + 1]) if node.level else ''
                    prefix = '.'.join(p for p in (base, node.module) if p)
                for alias in node.names:
                    if isinstance(node, ast.Import):
                        local = alias.asname or alias.name.split('.')[0]
                        full = alias.name if alias.asname else local
                        imported = alias.name
                    else:
                        local, full = alias.asname or alias.name, '.'.join(p for p in (prefix, alias.name) if p)
                        imported = full
                    scope['imports'].setdefault(local, []).append(full)
                    self.pending.append((sid, imported, self.evidence(path, node)))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    scope['locals'].update(n.id for n in ast.walk(target) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                scope['locals'].update(n.id for n in ast.walk(node.target) if isinstance(n, ast.Name))
        # A comprehensive scope-owned Store pass handles with/except/comprehensions conservatively.
        for statement in body:
            owned = [statement] if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [statement, *_owned_walk(statement)]
            for child in owned:
                if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                    scope['locals'].add(child.id)
                if isinstance(child, ast.ExceptHandler) and child.name:
                    scope['locals'].add(child.name)
                if isinstance(child, (ast.Global, ast.Nonlocal)):
                    scope['locals'].update(child.names)  # abstain on mutable nonlocal binding

    def contract(self, node, sid, path):
        args = node.args
        positional = args.posonlyargs + args.args
        defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        entries = [(a, 'positional_only' if i < len(args.posonlyargs) else 'positional_or_keyword', defaults[i])
                   for i, a in enumerate(positional)]
        if args.vararg:
            entries.append((args.vararg, 'var_positional', None))
        entries.extend((a, 'keyword_only', d) for a, d in zip(args.kwonlyargs, args.kw_defaults))
        if args.kwarg:
            entries.append((args.kwarg, 'var_keyword', None))
        params = []
        for a, kind, default in entries:
            p = {'name': a.arg, 'kind': kind, 'annotation': _expr(a.annotation),
                 'required': default is None and not kind.startswith('var_'),
                 'has_default': default is not None,
                 'default': _expr(default)}
            params.append(p)
            pid = self.add_node(sid + ':parameter:' + a.arg, 'parameter', **p, evidence=self.evidence(path, a))
            self.edge(sid, pid, 'accepts', self.evidence(path, a))
            self.scopes[sid]['locals'].add(a.arg)
        owned = list(_owned_walk(node))
        self.nodes[sid]['contract'] = {
            'parameters': params, 'return_annotation': _expr(node.returns),
            'return_expressions': [_expr(n.value) for n in owned if isinstance(n, ast.Return)],
            'yield_expressions': [_expr(n.value) for n in owned if isinstance(n, (ast.Yield, ast.YieldFrom))],
            'raises_observed': [_expr(n.exc) for n in owned if isinstance(n, ast.Raise)],
            'generator': any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in owned),
            'return_type_inference': 'not_performed', 'fallthrough_analysis': 'not_performed',
            'type_comment': getattr(node, 'type_comment', None),
            'type_parameters': [_expr(t) for t in getattr(node, 'type_params', [])],
        }
        key = path + ':' + self.nodes[sid]['qualname']
        capability = self.capabilities.get(key, self.capabilities.get(sid))
        self.nodes[sid]['capability'] = {'key': capability or 'python:' + node.name,
            'basis': 'explicit' if capability else 'name_heuristic'}

    def resolve_call(self, sid, expression):
        """Resolve syntactic binding candidates, avoiding unique-name global guesses."""
        if not expression:
            return [], 'dynamic'
        pieces = expression.split('.')
        first, suffix = pieces[0], '.'.join(pieces[1:])
        scope_id = sid
        while scope_id:
            scope = self.scopes[scope_id]
            # Instance dispatch can be overridden: always a candidate.
            if first in ('self', 'cls') and scope['class_id'] and suffix:
                class_node = self.nodes[scope['class_id']]
                full = '.'.join(p for p in (scope['dotted'], class_node['qualname'], suffix) if p)
                return self.symbols.get(full, []), 'method_candidate'
            if first in scope['locals']:
                return [], 'local_or_rebound'
            if first in scope['bindings']:
                ids = scope['bindings'][first]
                if suffix:
                    targets = []
                    for nid in ids:
                        full = '.'.join(p for p in (scope['dotted'], self.nodes[nid]['qualname'], suffix) if p)
                        targets.extend(self.symbols.get(full, []))
                    return targets, 'lexical_candidate'
                return ids, 'lexical_candidate'
            if first in scope['imports']:
                targets = []
                for imp in scope['imports'][first]:
                    full = imp + ('.' + suffix if suffix else '')
                    known = self.symbols.get(full, [])
                    if known:
                        targets.extend(known)
                    else:
                        nid = self.add_node('external:' + full, 'external', label=full,
                            resolution='imported_symbol_not_resolved_in_scanned_sources')
                        targets.append(nid)
                return targets, 'import_candidate'
            parent = scope['parent']
            # Python function free-name lookup skips the containing class namespace.
            if scope['kind'] == 'function':
                while parent and self.scopes[parent]['kind'] == 'class':
                    parent = self.scopes[parent]['parent']
            scope_id = parent
        if '.' not in expression:
            import builtins
            if hasattr(builtins, expression):
                return [self.add_node('external:builtins.' + expression, 'external', label='builtins.' + expression)], 'builtin_candidate'
        return [], 'unresolved'

    def extract(self):
        for py in discover_python(self.root, self.packages, self.excludes, self.extensions):
            path = py.relative_to(self.root).as_posix()
            try:
                raw = py.read_bytes()
                self.sources[path] = {'sha256': _digest(raw), 'size_bytes': len(raw)}
                with tokenize.open(py) as handle:
                    source = handle.read()
                tree = ast.parse(source, filename=path, type_comments=True)
            except (OSError, SyntaxError, UnicodeError, LookupError) as error:
                self.sources.setdefault(path, {'sha256': None})['status'] = 'unparseable'
                self.diagnostics.append({'kind': 'parse_error', 'path': path, 'message': str(error)})
                nid = self.add_node('module:' + path, 'module', path=path, status='unparseable')
                self.edge('repository:.', nid, 'contains')
                continue
            self.sources[path]['status'] = 'parsed'
            # --pkg src also acts as an import root for the common src layout.
            relative = Path(path)
            for package in sorted(self.packages, key=len, reverse=True):
                candidate = Path(package)
                if candidate.name == 'src' and relative.is_relative_to(candidate):
                    relative = relative.relative_to(candidate)
                    break
            dotted = _dotted(relative)
            sid = self.add_node('module:' + path, 'module', path=path, label=dotted,
                                status='parsed', docstring=ast.get_docstring(tree), evidence=self.evidence(path, tree))
            self.modules.setdefault(dotted, []).append(sid)
            self.symbol(dotted, sid)
            self.edge('repository:.', sid, 'contains', self.evidence(path, tree))
            self.scopes[sid] = {'parent': None, 'bindings': {}, 'imports': {}, 'locals': set(),
                               'class_id': None, 'kind': 'module', 'path': path, 'dotted': dotted}
            self.index_scope(tree.body, sid, path, dotted)
            self.units.append((path, tree, sid))
        for sid, full, ev in self.pending:
            targets = self.symbols.get(full, [])
            if not targets:
                targets = [self.add_node('external:' + full, 'external', label=full,
                            resolution='import_not_resolved_in_scanned_sources')]
            for target in targets:
                self.edge(sid, target, 'imports', ev, 'candidate', expression=full)
        for path, tree, sid in self.units:
            _BehaviorVisitor(self, path, sid).visit(tree)
        used = {n['capability']['key'] for n in self.nodes.values() if 'capability' in n and n['capability']['basis'] == 'explicit'}
        for key, label in self.capabilities.items():
            if label not in used:
                self.diagnostics.append({'kind': 'unused_capability_mapping', 'symbol': key, 'capability': label})
        edges = sorted({_json(e): e for e in self.edges}.values(), key=lambda e: (e['from'], e['rel'], e['to'], _json(e)))
        graph = {'schema': SCHEMA, 'language': 'python', 'generator': 'differential_graph.py',
                 'provenance': {'source_url': self.source_url, 'revision': self.revision,
                     'source_digest': _digest(_json(self.sources)), 'files': self.sources,
                     'license_status': 'not_assessed'},
                 'config': {'packages': self.packages, 'excludes': list(self.excludes), 'rules': self.rules},
                 'nodes': sorted(self.nodes.values(), key=lambda n: n['id']), 'edges': edges,
                 'diagnostics': self.diagnostics,
                 'limitations': ['Static observations do not prove execution, dominance, validation or exploitability.',
                     'Dynamic binding, aliases, middleware and external behavior are incomplete.',
                     'Effect tags and inferred capability labels are heuristics.']}
        graph['capabilities'] = capability_shapes(graph)
        graph['summary'] = {'node_count': len(graph['nodes']), 'edge_count': len(edges),
            'parsed_files': sum(s['status'] == 'parsed' for s in self.sources.values()),
            'unparseable_files': sum(s['status'] != 'parsed' for s in self.sources.values()),
            'unresolved_calls': sum(e['rel'] == 'calls' and e.get('resolution') in ('unresolved', 'dynamic', 'local_or_rebound') for e in edges)}
        validate_graph(graph)
        return graph


class _BehaviorVisitor(ast.NodeVisitor):
    def __init__(self, extractor, path, sid):
        self.g, self.path, self.sid = extractor, path, sid
        self.context = []
        self.counter = collections.Counter()

    def operation(self, node, rel, **fields):
        self.counter[(self.sid, rel, getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))] += 1
        count = self.counter[(self.sid, rel, getattr(node, 'lineno', 0), getattr(node, 'col_offset', 0))]
        ev = self.g.evidence(self.path, node)
        oid = f"operation:{self.sid}:{ev['line']}:{ev['column']}:{rel}:{count}"
        self.g.add_node(oid, 'operation', kind=rel, expression=_expr(node), evidence=ev,
                        lexical_context=list(self.context), **fields)
        self.g.edge(self.sid, oid, rel, ev, lexical_context=list(self.context))
        if rel in ('branches', 'asserts'):
            subject = node.test if isinstance(node, ast.Assert) else node
            self.g.nodes[oid]['predicate_shape'] = normalized_expression(subject)
        if rel in ('reads', 'returns', 'invokes', 'assigns'):
            # Scope-local parameter references, not value tracking or taint proof.
            for name in sorted({n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}):
                pid = self.sid + ':parameter:' + name
                if pid in self.g.nodes:
                    self.g.edge(oid, pid, 'references_parameter', ev, 'observed', semantics='syntactic_reference_only')
        return oid

    def visit_FunctionDef(self, node):
        child = self.g.ast_ids[id(node)]
        self.definition(node, child)
        # Defaults/decorator expressions execute in the enclosing scope.
        for default in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(default)
        old, context = self.sid, self.context
        self.sid, self.context = child, []
        for statement in node.body:
            self.visit(statement)
        self.sid, self.context = old, context

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        child = self.g.ast_ids[id(node)]
        self.definition(node, child)
        for base in node.bases:
            targets, resolution = self.g.resolve_call(self.sid, _name(base))
            if not targets:
                targets = [self.g.add_node('unresolved:base:' + child + ':' + str(base.lineno), 'unresolved', label=_expr(base))]
            for target in targets:
                self.g.edge(child, target, 'inherits', self.g.evidence(self.path, base), 'candidate', resolution=resolution, expression=_expr(base))
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        old = self.sid
        self.sid = child
        for statement in node.body:
            self.visit(statement)
        self.sid = old

    def definition(self, node, child):
        for decorator in node.decorator_list:
            expr = decorator.func if isinstance(decorator, ast.Call) else decorator
            targets, resolution = self.g.resolve_call(self.sid, _name(expr))
            if not targets:
                targets = [self.g.add_node('unresolved:decorator:' + child + ':' + str(decorator.lineno), 'unresolved', label=_expr(expr))]
            for target in targets:
                self.g.edge(child, target, 'decorates', self.g.evidence(self.path, decorator), 'candidate',
                            expression=_expr(decorator), resolution=resolution)
            self.route(decorator, child)
            self.visit(decorator)

    def route(self, node, child):
        if not isinstance(node, ast.Call):
            return
        spelling = _name(node.func)
        verb = spelling.rsplit('.', 1)[-1].lower()
        if verb not in ('route', 'get', 'post', 'put', 'patch', 'delete', 'head', 'options', 'websocket'):
            return
        path_arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg in ('path', 'rule')), None)
        if path_arg is None:
            return
        literal = _literal(path_arg)
        if not literal.get('known') or not isinstance(literal['value'], str):
            return
        route = literal['value']
        methods = [verb.upper()] if verb != 'route' else ['UNKNOWN']
        for kw in node.keywords:
            if kw.arg == 'methods':
                val = _literal(kw.value)
                if val.get('known') and isinstance(val['value'], (list, tuple)) and all(isinstance(v, str) for v in val['value']):
                    methods = sorted(set(v.upper() for v in val['value']))
        norm = re.sub(r'\{[^}]+\}|<[^>]+>', '{}', route)
        for method in methods:
            eid = self.g.add_node('endpoint:' + method + ':' + route, 'endpoint', method=method,
                                 path=route, normalized_path=norm, evidence=self.g.evidence(self.path, node))
            self.g.edge(child, eid, 'exposes', self.g.evidence(self.path, node), 'inferred',
                        rule='route_decorator_spelling', decorator=spelling)
        card = self.g.nodes[child]
        if 'capability' in card and card['capability']['basis'] != 'explicit':
            card['capability'] = {'key': 'http:' + ','.join(methods) + ':' + norm, 'basis': 'route_heuristic'}

    def visit_Call(self, node):
        spelling = _name(node.func)
        targets, resolution = self.g.resolve_call(self.sid, spelling)
        ev = self.g.evidence(self.path, node)
        if not targets:
            targets = [self.g.add_node(f"unresolved:{self.sid}:{ev['line']}:{ev['column']}", 'unresolved',
                        label=_expr(node.func), reason=resolution, evidence=ev)]
        labels = [spelling] + [self.g.nodes[t].get('label', '') for t in targets]
        tags = sorted(tag for tag, patterns in self.g.rules.items()
                      if any(fnmatch.fnmatchcase(label.lower(), p.lower()) for label in labels for p in patterns))
        oid = self.operation(node, 'invokes', tags=tags, callee_expression=_expr(node.func),
                             arguments=[{'expression': _expr(a), 'starred': isinstance(a, ast.Starred)} for a in node.args],
                             keywords=[{'name': k.arg, 'expression': _expr(k.value)} for k in node.keywords])
        for target in targets:
            self.g.edge(self.sid, target, 'calls', ev, 'candidate', resolution=resolution,
                        callsite=oid, expression=_expr(node.func), tags=tags,
                        lexical_context=list(self.context))
        if spelling.endswith('.add_parser') and node.args:
            value = _literal(node.args[0])
            if value.get('known') and isinstance(value['value'], str):
                cid = self.g.add_node('command:' + value['value'], 'command', label=value['value'])
                self.g.edge(self.sid, cid, 'registers_command', ev, 'inferred', reachability='unknown')
        self.generic_visit(node)

    def block(self, nodes, descriptor):
        self.context.append(descriptor)
        for node in nodes:
            self.visit(node)
        self.context.pop()

    def visit_If(self, node):
        oid = self.operation(node.test, 'branches', branch_kind='if')
        self.visit(node.test)
        self.block(node.body, {'operation': oid, 'arm': 'true', 'predicate': _expr(node.test)})
        self.block(node.orelse, {'operation': oid, 'arm': 'false', 'predicate': _expr(node.test)})

    def visit_IfExp(self, node):
        oid = self.operation(node.test, 'branches', branch_kind='conditional_expression')
        self.visit(node.test)
        self.block([node.body], {'operation': oid, 'arm': 'true'})
        self.block([node.orelse], {'operation': oid, 'arm': 'false'})

    def visit_BoolOp(self, node):
        oid = self.operation(node, 'branches', branch_kind=type(node.op).__name__)
        for index, value in enumerate(node.values):
            self.block([value], {'operation': oid, 'arm': f'operand:{index}', 'short_circuit': True})

    def visit_Assert(self, node):
        self.operation(node, 'asserts', predicate=_expr(node.test), disabled_by_optimization=True)
        self.generic_visit(node)

    def visit_For(self, node):
        oid = self.operation(node, 'loops', loop_kind=type(node).__name__)
        self.visit(node.iter)
        self.visit(node.target)
        self.block(node.body, {'operation': oid, 'arm': 'body'})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})

    visit_AsyncFor = visit_For

    def visit_While(self, node):
        oid = self.operation(node.test, 'loops', loop_kind='While')
        self.visit(node.test)
        self.block(node.body, {'operation': oid, 'arm': 'body'})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})

    def visit_Try(self, node):
        oid = self.operation(node, 'tries')
        self.block(node.body, {'operation': oid, 'arm': 'try'})
        for handler in node.handlers:
            self.operation(handler, 'handles', exception=_expr(handler.type), binding=handler.name,
                           exception_group=type(node).__name__ == 'TryStar')
            self.block(handler.body, {'operation': oid, 'arm': 'except', 'exception': _expr(handler.type)})
        self.block(node.orelse, {'operation': oid, 'arm': 'else'})
        self.block(node.finalbody, {'operation': oid, 'arm': 'finally'})

    visit_TryStar = visit_Try

    def visit_With(self, node):
        oid = self.operation(node, 'uses_context', managers=[_expr(i.context_expr) for i in node.items],
                             async_context=isinstance(node, ast.AsyncWith))
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self.visit(item.optional_vars)
        self.block(node.body, {'operation': oid, 'arm': 'body'})

    visit_AsyncWith = visit_With

    def visit_Match(self, node):
        oid = self.operation(node.subject, 'branches', branch_kind='match')
        self.visit(node.subject)
        for case in node.cases:
            descriptor = {'operation': oid, 'arm': _expr(case.pattern), 'guard': _expr(case.guard)}
            self.block(([case.guard] if case.guard else []) + case.body, descriptor)

    def visit_Lambda(self, node):
        # Explicit coverage boundary; do not fabricate calls in the enclosing scope.
        self.operation(node, 'defines_lambda', analysis='body_not_analyzed')
        self.g.diagnostics.append({'kind': 'lambda_body_not_analyzed', 'evidence': self.g.evidence(self.path, node)})
        for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
            self.visit(default)

    def visit_Name(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='name', name=node.id)

    def visit_Attribute(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='attribute', name=_expr(node))
        self.visit(node.value)

    def visit_Subscript(self, node):
        self.operation(node, 'writes' if isinstance(node.ctx, ast.Store) else 'deletes' if isinstance(node.ctx, ast.Del) else 'reads',
                       access_kind='subscript', base=_expr(node.value), key=_expr(node.slice))
        self.generic_visit(node)

    def visit_Assign(self, node):
        self.operation(node, 'assigns', targets=[_expr(t) for t in node.targets], value=_expr(node.value),
                       source_names=sorted({n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}),
                       flow='syntactic_only')
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if self.g.scopes[self.sid]['kind'] in ('class', 'module') and isinstance(node.target, ast.Name):
            fid = self.g.add_node(self.sid + ':field:' + node.target.id, 'field',
                label=node.target.id, annotation=_expr(node.annotation), default=_expr(node.value),
                evidence=self.g.evidence(self.path, node))
            self.g.edge(self.sid, fid, 'declares_field', self.g.evidence(self.path, node))
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value), annotation=_expr(node.annotation))
        self.visit(node.target)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node):
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value),
                       operator=type(node.op).__name__, reads_previous_value=True)
        self.operation(node.target, 'reads', access_kind='augmented_target', name=_expr(node.target))
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        self.operation(node, 'assigns', targets=[_expr(node.target)], value=_expr(node.value), walrus=True)
        self.generic_visit(node)

    def visit_ListComp(self, node):
        oid = self.operation(node, 'comprehends', deferred=isinstance(node, ast.GeneratorExp))
        self.context.append({'operation': oid, 'arm': 'comprehension', 'execution': 'possibly_deferred'})
        self.generic_visit(node)
        self.context.pop()

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Return(self, node):
        self.operation(node, 'returns', value=_expr(node.value))
        self.generic_visit(node)

    def visit_Raise(self, node):
        self.operation(node, 'raises', exception=_expr(node.exc), cause=_expr(node.cause), reraises=node.exc is None)
        self.generic_visit(node)

    def visit_Yield(self, node):
        self.operation(node, 'yields', value=_expr(node.value), delegated=isinstance(node, ast.YieldFrom))
        self.generic_visit(node)

    visit_YieldFrom = visit_Yield

    def visit_Await(self, node):
        self.operation(node, 'awaits', value=_expr(node.value))
        self.generic_visit(node)


def extract_python(root, **kwargs):
    return PythonGraphExtractor(root, **kwargs).extract()


def validate_graph(graph):
    if graph.get('schema') not in (SCHEMA, 'language-evidence-graph/2.0'):
        raise ValueError('Unsupported graph schema')
    ids = [n['id'] for n in graph['nodes']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate node IDs')
    known = set(ids)
    for edge in graph['edges']:
        if edge['from'] not in known or edge['to'] not in known:
            raise ValueError(f'Dangling edge: {edge}')
    return True


def normalized_expression(node):
    """Alpha-normalize names within one expression; preserve operators and literals.

    This is structural normalization, not semantic equivalence. Attribute names
    remain visible; variable placeholders preserve repeated-name relationships.
    """
    mapping = {}
    def shape(value):
        if isinstance(value, ast.Name):
            return ['Name', mapping.setdefault(value.id, 'v' + str(len(mapping)))]
        if isinstance(value, ast.AST):
            return [type(value).__name__, {field: shape(child) for field, child in ast.iter_fields(value)
                                           if field not in ('ctx', 'type_comment')}]
        if isinstance(value, list):
            return [shape(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)
    return json.dumps(shape(node), sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def capability_shapes(graph):
    """Scope-local, explainable signatures; do not confuse hashes with semantics."""
    nodes = {n['id']: n for n in graph['nodes']}
    outgoing = collections.defaultdict(list)
    for edge in graph['edges']:
        outgoing[edge['from']].append(edge)
    shapes = []
    for node in graph['nodes']:
        if 'capability' not in node:
            continue
        contract = node.get('contract', {'parameters': [], 'return_annotation': None, 'generator': False})
        features = set()
        for i, param in enumerate(contract['parameters']):
            features.add(f"parameter:{i}:{param['kind']}:{'unknown' if param.get('required') is None else 'required' if param['required'] else 'optional'}")
            if param['annotation']:
                features.add(f"parameter_annotation:{i}:{param['annotation']}")
        if contract['return_annotation']:
            features.add('return_annotation:' + contract['return_annotation'])
        if node.get('async'):
            features.add('async')
        if contract['generator']:
            features.add('generator')
        neighborhood, operations, feature_evidence = [], [], collections.defaultdict(list)
        for edge in outgoing[node['id']]:
            target = nodes[edge['to']]
            neighborhood.append({'rel': edge['rel'], 'target_type': target['type'],
                                  'confidence': edge['confidence'], 'target': target['id']})
            if target['type'] == 'operation':
                operations.append(target)
                feature = 'operation:' + target['kind']
                features.add(feature)
                feature_evidence[feature].append(target['evidence'])
                if target.get('predicate_shape'):
                    guard_feature = 'predicate_shape:' + target['predicate_shape']
                    features.add(guard_feature)
                    feature_evidence[guard_feature].append(target['evidence'])
                if target['kind'] == 'raises':
                    exc = target.get('exception')
                    raise_feature = 'raises:' + (exc.split('(')[0] if exc else '<reraise>')
                    features.add(raise_feature)
                    feature_evidence[raise_feature].append(target['evidence'])
                for tag in target.get('tags', []):
                    features.add('tag:' + tag)
                    feature_evidence['tag:' + tag].append(target['evidence'])
            for tag in edge.get('tags', []):
                features.add('tag:' + tag)
                if edge.get('evidence'):
                    feature_evidence['tag:' + tag].append(edge['evidence'])
            if edge['rel'] not in ('contains', 'accepts'):
                feature = 'edge:' + edge['rel'] + ':' + target['type']
                features.add(feature)
                if edge.get('evidence'):
                    feature_evidence[feature].append(edge['evidence'])
            if target['type'] == 'operation' and target.get('kind') == 'declares_style':
                feature = 'style_property:' + target.get('name', '')
                features.add(feature)
                feature_evidence[feature].append(target['evidence'])
            if edge['rel'] in ('decorates', 'exposes'):
                feature = edge['rel'] + ':' + (edge.get('expression') or target.get('method', '') + ':' + target.get('normalized_path', ''))
                features.add(feature)
                feature_evidence[feature].append(edge['evidence'])
            if edge['rel'] == 'calls' and target.get('capability', {}).get('basis') == 'explicit':
                feature = 'calls_capability:' + target['capability']['key']
                features.add(feature)
                feature_evidence[feature].append(edge['evidence'])
        shapes.append({'node': node['id'], **node['capability'], 'evidence': node['evidence'],
                       'contract': contract, 'features': sorted(features),
                       'feature_evidence': dict(feature_evidence),
                       'neighborhood': neighborhood,
                       'operations': [n['id'] for n in sorted(operations, key=lambda n: (n['evidence']['line'], n['evidence']['column'], n['id']))],
                       'feature_digest': _digest(_json(sorted(features)))})
    return sorted(shapes, key=lambda s: (s['key'], s['node']))


def compare_graphs(target, peers, threshold=0.8, min_peers=2, allow_inferred=False):
    """Return missing/extra observed features, with explicit voting denominators."""
    if not 0 < threshold <= 1 or min_peers < 1:
        raise ValueError('threshold must be in (0,1]; min_peers must be positive')
    for graph in [target, *peers]:
        validate_graph(graph)
    excluded = []
    def index(graph):
        grouped = collections.defaultdict(list)
        for shape in capability_shapes(graph):
            if shape['basis'] == 'explicit' or allow_inferred:
                grouped[shape['key']].append(shape)
        return {key: values[0] for key, values in grouped.items() if len(values) == 1}, sorted(k for k, v in grouped.items() if len(v) > 1)
    own, ambiguous = index(target)
    unique, seen = [], {target['provenance']['source_digest']}
    for i, graph in enumerate(peers):
        digest = graph['provenance']['source_digest']
        if digest in seen:
            excluded.append({'peer': i, 'reason': 'duplicate_source_digest'})
            continue
        seen.add(digest)
        if graph['config'].get('rules') != target['config'].get('rules'):
            excluded.append({'peer': i, 'reason': 'different_effect_rules'})
            continue
        indexed, duplicates = index(graph)
        unique.append((i, indexed, duplicates, graph['provenance']))
    findings, coverage = [], []
    for key, ours in sorted(own.items()):
        aligned = [(i, idx[key], provenance) for i, idx, _, provenance in unique if key in idx]
        coverage.append({'capability': key, 'eligible_peers': len(aligned),
                         'excluded_or_unaligned_peers': len(peers) - len(aligned)})
        if len(aligned) < min_peers:
            continue
        counts = collections.Counter(f for _, shape, _ in aligned for f in set(shape['features']))
        ours_features = set(ours['features'])
        for feature in sorted(set(counts) | ours_features):
            support = counts[feature]
            prevalence = support / len(aligned)
            direction = 'not_observed_in_target' if feature not in ours_features and prevalence >= threshold else (
                'target_outlier_presence' if feature in ours_features and (1 - prevalence) >= threshold else None)
            if direction:
                findings.append({'capability': key, 'target_node': ours['node'], 'feature': feature,
                    'direction': direction, 'classification': 'review_candidate',
                    'peer_support': support, 'eligible_peers': len(aligned), 'prevalence': prevalence,
                    'outlier_score': prevalence if direction == 'not_observed_in_target' else 1 - prevalence,
                    'target_evidence': ours['evidence'],
                    'peer_evidence': [{'peer': i, 'source_digest': provenance['source_digest'],
                        'source_url': provenance.get('source_url'), 'revision': provenance.get('revision'),
                        'observed': feature in shape['features'],
                        'evidence': shape['feature_evidence'].get(feature, [shape['evidence']])}
                        for i, shape, provenance in aligned],
                    'caveat': 'Observation difference only; inspect call chains, middleware, specs and extraction coverage.'})
    return {'schema': 'python-differential-review/1.0',
            'findings': sorted(findings, key=lambda f: (-f['outlier_score'], f['capability'], f['feature'])),
            'coverage': coverage, 'excluded_peers': excluded, 'ambiguous_target_capabilities': ambiguous,
            'ambiguous_peer_capabilities': [{'peer': i, 'keys': d} for i, _, d, _ in unique if d],
            'alignment_policy': 'exact_key_including_heuristics' if allow_inferred else 'explicit_only',
            'note': 'No aligned vote is not a clean audit. Consensus is not a correctness oracle.'}


def query_graph(graph, command, ref, other=None, rels=None, depth=1):
    validate_graph(graph)
    nodes = {n['id']: n for n in graph['nodes']}
    def hits(value):
        if value in nodes:
            return [value]
        exact = sorted(nid for nid, n in nodes.items() if value in (n.get('label'), n.get('qualname'), n.get('capability', {}).get('key')))
        return exact or sorted(nid for nid, n in nodes.items() if value.lower() in nid.lower())
    matches = hits(ref)
    if command == 'find':
        return {'query': ref, 'matches': matches}
    if len(matches) != 1:
        raise ValueError(f'Reference must resolve uniquely: {ref!r}; candidates={matches}')
    start = matches[0]
    if command == 'seam':
        return {'node': nodes[start], 'edges': [e for e in graph['edges'] if e['from'] == start or e['to'] == start]}
    if command == 'where':
        return {'node': start, 'evidence': nodes[start].get('evidence')}
    outgoing, incoming = collections.defaultdict(set), collections.defaultdict(set)
    for edge in graph['edges']:
        if not rels or edge['rel'] in rels:
            outgoing[edge['from']].add(edge['to'])
            incoming[edge['to']].add(edge['from'])
    if command == 'path':
        ends = hits(other or '')
        if len(ends) != 1:
            raise ValueError(f'Destination must resolve uniquely: {other!r}; candidates={ends}')
        end = ends[0]
        queue, parent = collections.deque([start]), {start: None}
        while queue:
            current = queue.popleft()
            if current == end:
                result = []
                while current is not None:
                    result.append(current)
                    current = parent[current]
                return {'from': start, 'to': end, 'path': result[::-1]}
            for nxt in sorted(outgoing[current]):
                if nxt not in parent:
                    parent[nxt] = current
                    queue.append(nxt)
        return {'from': start, 'to': end, 'path': None}
    adjacency = incoming if command in ('who', 'blast') else outgoing
    seen, frontier, levels = {start}, {start}, []
    for _ in range(max(0, depth)):
        nxt = set().union(*(adjacency[n] for n in frontier)) - seen
        if not nxt:
            break
        levels.append(sorted(nxt))
        seen.update(nxt)
        frontier = nxt
    return {'node': start, 'levels': levels, 'count': len(seen) - 1}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents partially written graphs after interruptions.
    import tempfile
    temp = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, delete=False) as handle:
            temp = Path(handle.name)
            handle.write(_json(value))
        temp.replace(path)
    finally:
        if temp and temp.exists():
            temp.unlink()

# === Declarative language registry and generic adapters ===
MULTI_SCHEMA = 'language-evidence-graph/2.0'
BACKENDS = {'python_ast', 'regex_balanced', 'html'}
REGISTRY_SCHEMA = 'language-core/1.0'


def _compiled(pattern):
    return re.compile(pattern, re.MULTILINE)


def load_language_core(path=None):
    """Validate a trusted local rule library; never evaluate formula strings."""
    path = Path(path) if path else Path(__file__).with_name('lang_core.json')
    core = json.loads(path.read_text(encoding='utf-8'))
    if core.get('schema') != REGISTRY_SCHEMA or not isinstance(core.get('languages'), dict):
        raise ValueError('Expected language-core/1.0 and a languages object')
    for language, profile in core['languages'].items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]*', language):
            raise ValueError(f'Invalid language ID: {language!r}')
        if profile.get('backend') not in BACKENDS:
            raise ValueError(f'{language}: unsupported backend')
        if profile['backend'] in ('python_ast', 'html') and language != {'python_ast': 'python', 'html': 'html'}[profile['backend']]:
            raise ValueError('Native adapters must retain their canonical language ID')
        if 'parameter_pattern' in profile:
            _compiled(profile['parameter_pattern'])
        extensions = profile.get('extensions')
        if not isinstance(extensions, list) or not extensions or not all(
                isinstance(e, str) and re.fullmatch(r'\.[A-Za-z0-9.]+', e) for e in extensions):
            raise ValueError(f'{language}: extensions must be a nonempty list of suffixes')
        for category in ('comments', 'strings'):
            for pattern in profile.get('lexical', {}).get(category, []):
                if _compiled(pattern).match(''):
                    raise ValueError(f'{language}: empty lexical token')
        pairs = profile.get('lexical', {}).get('brackets', [])
        if any(len(pair) != 2 or any(not isinstance(v, str) or len(v) != 1 for v in pair) for pair in pairs):
            raise ValueError(f'{language}: bracket pairs must contain two single characters')
        rule_ids = set()
        for rule in profile.get('rules', []):
            rid = rule.get('id')
            if not isinstance(rid, str) or not rid or rid in rule_ids:
                raise ValueError(f'{language}: duplicate/invalid rule ID {rid!r}')
            rule_ids.add(rid)
            if rule.get('action') not in ('scope', 'reference', 'observation'):
                raise ValueError(f'{rid}: unknown action')
            if rule.get('stream', 'code') not in ('code', 'comments_masked'):
                raise ValueError(f'{rid}: unknown stream')
            compiled = _compiled(rule['pattern'])
            if compiled.match(''):
                raise ValueError(f'{rid}: rule must not match empty text')
            if rule['action'] in ('scope', 'reference') and 'name' not in compiled.groupindex:
                raise ValueError(f'{rid}: named group "name" required')
            if not isinstance(rule.get('relation'), str) or not rule['relation']:
                raise ValueError(f'{rid}: relation required')
            if 'body_gap' in rule:
                _compiled(rule['body_gap'])
            for pair_name in ('arguments', 'body'):
                if pair_name in rule and (len(rule[pair_name]) != 2 or any(len(c) != 1 for c in rule[pair_name])):
                    raise ValueError(f'{rid}: invalid {pair_name} delimiter pair')
            if 'capability' in rule:
                import string
                fields = [field for _, field, _, _ in string.Formatter().parse(rule['capability']) if field]
                if any(field not in ('language', 'name') for field in fields):
                    raise ValueError(f'{rid}: capability template allows language and name only')
        for region in [profile['regions']] if 'regions' in profile else []:
            for field in ('start_pattern', 'end_pattern'):
                if _compiled(region[field]).match(''):
                    raise ValueError(f'{language}: empty region marker')
        effects = profile.get('effects', {})
        if not isinstance(effects, dict) or not all(isinstance(v, list) and all(isinstance(p, str) for p in v) for v in effects.values()):
            raise ValueError(f'{language}: effects must map labels to pattern lists')
    return core


def _blank(text):
    return ''.join('\n' if c == '\n' else '\r' if c == '\r' else ' ' for c in text)


def lexical_views(text, lexical):
    """Offset-preserving masks. Tokens follow registry priority at equal starts."""
    tokens = [('comment', p) for p in lexical.get('comments', [])]
    tokens += [('string', p) for p in lexical.get('strings', [])]
    code, comments = list(text), list(text)
    if not tokens:
        return text, text
    tokenizer = re.compile('|'.join(f'(?P<T{i}>{p})' for i, (_, p) in enumerate(tokens)), re.MULTILINE)
    for match in tokenizer.finditer(text):
        index = int(match.lastgroup[1:])
        blank = _blank(match.group())
        code[match.start():match.end()] = blank
        if tokens[index][0] == 'comment':
            comments[match.start():match.end()] = blank
    return ''.join(code), ''.join(comments)


def bracket_pairs(code, pairs):
    opening = dict(pairs)
    closing = {v: k for k, v in pairs}
    stack, result, bad = [], {}, []
    for offset, char in enumerate(code):
        if char in opening:
            stack.append((char, offset))
        elif char in closing:
            if stack and stack[-1][0] == closing[char]:
                _, start = stack.pop()
                result[start] = offset
            else:
                bad.append(offset)
    bad.extend(start for _, start in stack)
    return result, sorted(bad)


def delimited_regions(text, region, lexical):
    """Return body spans; closing delimiters inside strings/comments are ignored."""
    start_re, end_re = _compiled(region['start_pattern']), _compiled(region['end_pattern'])
    cursor, spans = 0, []
    while match := start_re.search(text, cursor):
        start = match.end()
        code, _ = lexical_views(text[start:], lexical)
        end_match = end_re.search(code)
        if end_match:
            end = start + end_match.start()
            cursor = start + end_match.end()
        else:
            end, cursor = len(text), len(text)
        spans.append({'start': start, 'end': end, 'full_start': match.start(), 'full_end': cursor,
                      'closed': end_match is not None})
        if not end_match:
            break
    return spans


class _EvidenceGraph:
    def __init__(self, core, selected, capabilities):
        self.core, self.selected, self.capabilities = core, selected, capabilities
        self.nodes = {'repository:.': {'id': 'repository:.', 'type': 'repository', 'label': 'repository'}}
        self.edges, self.files, self.diagnostics = [], {}, []
        self.texts, self.line_starts = {}, {}

    def source(self, path, raw, text):
        self.texts[path] = text
        self.line_starts[path] = [0] + [m.end() for m in re.finditer('\n', text)]
        self.files[path] = {'sha256': _digest(raw), 'size_bytes': len(raw), 'status': 'parsed', 'analyses': []}

    def evidence(self, path, start, end):
        lines = self.line_starts[path]
        row = bisect.bisect_right(lines, start) - 1
        end_row = bisect.bisect_right(lines, end) - 1
        return {'path': path, 'line': row + 1, 'column': start - lines[row],
                'end_line': end_row + 1, 'end_column': end - lines[end_row],
                'start_offset': start, 'end_offset': end, 'column_unit': 'unicode_codepoint',
                'source_sha256': self.files[path]['sha256']}

    def node(self, nid, node_type, **fields):
        self.nodes.setdefault(nid, {'id': nid, 'type': node_type, **fields})
        return nid

    def edge(self, source, target, rel, evidence=None, confidence='inferred', **fields):
        self.edges.append({'from': source, 'to': target, 'rel': rel, 'confidence': confidence,
                           **({'evidence': evidence} if evidence else {}), **fields})

    def diag(self, kind, path, start=0, end=0, **fields):
        self.diagnostics.append({'kind': kind, 'evidence': self.evidence(path, start, end), **fields})

    def view(self, path, language, start, end, parent=None):
        module = self.node('module:' + path, 'module', path=path, label=path)
        self.edge('repository:.', module, 'contains', self.evidence(path, 0, len(self.texts[path])), 'observed')
        vid = f'view:{language}:{path}:{start}:{end}'
        self.node(vid, 'language_view', language=language, path=path,
                  evidence=self.evidence(path, start, end), label=language)
        self.edge(parent or module, vid, 'contains', self.evidence(path, start, end), 'observed')
        return vid

    def capability(self, path, qualname, nid, language, template=None):
        key = self.capabilities.get(nid, self.capabilities.get(path + ':' + qualname))
        return {'key': key or (template or '{language}:{name}').format(language=language, name=qualname),
                'basis': 'explicit' if key else 'name_heuristic'}


def _split_arguments(raw, lexical):
    code, _ = lexical_views(raw, lexical)
    pairs, _ = bracket_pairs(code, lexical.get('brackets', []))
    result, start, i = [], 0, 0
    while i < len(code):
        if i in pairs:
            i = pairs[i] + 1
            continue
        if code[i] == ',':
            result.append(raw[start:i].strip())
            start = i + 1
        i += 1
    if raw[start:].strip():
        result.append(raw[start:].strip())
    return result


def _parameter_shape(raw, profile):
    shape = {'name': raw, 'raw': raw, 'kind': 'unknown', 'required': None,
             'annotation': None, 'default': None, 'fidelity': 'raw'}
    pattern = profile.get('parameter_pattern')
    match = _compiled(pattern).fullmatch(raw) if pattern else None
    if match:
        groups = match.groupdict()
        variadic = bool(groups.get('variadic'))
        shape.update(name=groups.get('name') or raw, annotation=groups.get('annotation'),
                     default=groups.get('default'), required=not variadic and groups.get('default') is None,
                     kind='var_positional' if variadic else 'positional_or_keyword', fidelity='pattern')
    return shape


def scan_regex(graph, path, language, start=0, end=None, parent=None, source_override=None):
    """Interpret registry scope/reference/observation formulas over one region."""
    profile = graph.core['languages'][language]
    text = graph.texts[path]
    end = len(text) if end is None else end
    source = (source_override if source_override is not None else text)[start:end]
    code, comments = lexical_views(source, profile.get('lexical', {}))
    pairs, bad = bracket_pairs(code, profile.get('lexical', {}).get('brackets', []))
    owner_view = graph.view(path, language, start, end, parent)
    for offset in bad[:50]:
        graph.diag('unbalanced_delimiter', path, start + offset, start + offset + 1, language=language)
    graph.files[path]['analyses'].append({'language': language, 'backend': 'regex_balanced',
        'start_offset': start, 'end_offset': end, 'coverage': 'partial', 'unbalanced_delimiters': len(bad)})
    matches = []
    for rule in profile.get('rules', []):
        stream = code if rule.get('stream', 'code') == 'code' else comments
        for match in _compiled(rule['pattern']).finditer(stream):
            values = {k: (source[match.start(k):match.end(k)].strip() if v is not None else None) for k, v in match.groupdict().items()}
            if values.get('name') in rule.get('exclude_names', []):
                continue
            # A literal containing an entire declaration/call is not executable code.
            first = match.start()
            while first < match.end() and source[first].isspace():
                first += 1
            if first < len(code) and code[first].isspace() and not source[first].isspace():
                continue
            matches.append((rule, match, values))
    scopes, headers = [], []
    for rule, match, values in matches:
        if rule['action'] != 'scope':
            continue
        body_start = match.start('body') if values.get('body') else None
        params = None
        if 'arguments' in rule:
            arg_start = match.start('args') if values.get('args') else code.find(rule['arguments'][0], match.start(), match.end())
            arg_end = pairs.get(arg_start)
            if arg_end is None:
                graph.diag('unresolved_signature', path, start + match.start(), start + match.end(), rule=rule['id'])
                continue
            params = source[arg_start + 1:arg_end]
            gap = _compiled(rule.get('body_gap', r'\s*')).match(code, arg_end + 1)
            body_start = gap.end() if gap else None
            if body_start is None or body_start >= len(code) or code[body_start] != rule.get('body', ['{'])[0]:
                graph.diag('unsupported_definition_body', path, start + match.start(), start + arg_end + 1, rule=rule['id'])
                continue
        if body_start is None or body_start not in pairs:
            graph.diag('unresolved_scope', path, start + match.start(), start + match.end(), rule=rule['id'])
            continue
        body_end = pairs[body_start]
        name_start = match.start('name')
        if any(s['name_start'] == name_start and s['body_start'] == body_start for s in scopes):
            continue
        scopes.append({'rule': rule, 'match': match, 'values': values, 'name_start': name_start,
                       'body_start': body_start, 'body_end': body_end, 'params': params})
        headers.append((name_start, body_start))
    scopes.sort(key=lambda s: (s['body_start'], -s['body_end']))
    for scope in scopes:
        rule, values = scope['rule'], scope['values']
        containers = [s for s in scopes if 'id' in s and s['body_start'] < scope['name_start'] < s['body_end']]
        container = min(containers, key=lambda s: s['body_end'] - s['body_start']) if containers else None
        qual = (container['qualname'] + '.' if container else '') + values['name']
        nid = f'symbol:{path}:{qual}'
        if nid in graph.nodes:
            nid += f'@{language}:{start + scope["name_start"]}'
        scope.update(id=nid, qualname=qual)
        ev = graph.evidence(path, start + scope['name_start'], start + scope['body_end'] + 1)
        kind = rule.get('node_type', 'function')
        fields = {'label': values['name'], 'qualname': qual, 'language': language, 'path': path,
                  'evidence': ev, 'rule': rule['id'], 'confidence': 'inferred',
                  'header': source[scope['match'].start():scope['body_start']],
                  'body_evidence': graph.evidence(path, start + scope['body_start'], start + scope['body_end'] + 1)}
        if kind in ('function', 'style_rule'):
            fields['capability'] = graph.capability(path, qual, nid, language, rule.get('capability'))
        if kind == 'function':
            parameters = _split_arguments(scope['params'] or '', profile.get('lexical', {}))
            fields['async'] = bool(re.search(r'\basync\b', fields['header']))
            fields['contract'] = {'parameters': [_parameter_shape(p, profile) for p in parameters],
                'return_annotation': None, 'return_expressions': [], 'raises_observed': [],
                'generator': False, 'yield_expressions': [], 'signature_fidelity': 'pattern_matched_or_raw_unknown',
                'return_type_inference': 'not_performed', 'fallthrough_analysis': 'not_performed'}
        graph.node(nid, kind, **fields)
        graph.edge(container['id'] if container else owner_view, nid, rule['relation'], ev, rule=rule['id'])
        if kind == 'function':
            for i, param in enumerate(fields['contract']['parameters']):
                pid = graph.node(nid + f':parameter:{i}', 'parameter', language=language, evidence=ev, **param)
                graph.edge(nid, pid, 'accepts', ev, rule=rule['id'])
        if values.get('base'):
            base = graph.node(f'external:{language}:{values["base"]}', 'external', label=values['base'], language=language)
            graph.edge(nid, base, 'inherits', ev, 'candidate', rule=rule['id'])
    def owner(offset):
        containing = [s for s in scopes if s['body_start'] < offset < s['body_end']]
        return min(containing, key=lambda s: s['body_end'] - s['body_start'])['id'] if containing else owner_view
    for rule, match, values in matches:
        if rule['action'] == 'scope':
            continue
        position = match.start('name') if values.get('name') else match.start()
        if rule.get('relation') == 'calls' and any(a <= position <= b for a, b in headers):
            continue
        sid, ev = owner(position), graph.evidence(path, start + match.start(), start + match.end())
        if rule['action'] == 'reference':
            name = values['name']
            kind = rule.get('node_type', 'external')
            target = f'{kind}:{language}:{name}'
            if kind == 'unresolved':
                target += ':' + path + ':' + str(start + position)
            tags = sorted(tag for tag, patterns in profile.get('effects', {}).items()
                          if any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in patterns))
            attributes = dict(rule.get('attributes', {}))
            if values.get('method'):
                attributes['method'] = values['method'].upper()
                target += ':' + attributes['method']
            graph.node(target, kind, label=name, language=language, evidence=ev, **attributes)
            graph.edge(sid, target, rule['relation'], ev, 'candidate', rule=rule['id'], tags=tags,
                       expression=source[match.start():match.end()], resolution='unresolved' if kind == 'unresolved' else 'syntactic_reference')
        else:
            oid = f'operation:{language}:{path}:{start + match.start()}:{rule["id"]}'
            attributes = {k: v for k, v in values.items() if v is not None}
            if 'arguments' in rule:
                arg_start = code.find(rule['arguments'][0], match.start(), match.end())
                arg_end = pairs.get(arg_start)
                if arg_end is not None:
                    attributes['predicate'] = source[arg_start + 1:arg_end]
                    ev = graph.evidence(path, start + match.start(), start + arg_end + 1)
            graph.node(oid, 'operation', kind=rule['relation'], language=language, evidence=ev,
                       expression=source[match.start():match.end()], rule=rule['id'], **attributes)
            graph.edge(sid, oid, rule['relation'], ev, rule=rule['id'])
            contract = graph.nodes[sid].get('contract')
            if contract is not None and rule['relation'] in ('returns', 'raises'):
                contract['return_expressions' if rule['relation'] == 'returns' else 'raises_observed'].append(source[match.start():match.end()])
    return owner_view


class _HTMLAdapter(HTMLParser):
    def __init__(self, graph, path, profile, text):
        super().__init__(convert_charrefs=True)
        self.graph, self.path, self.profile = graph, path, profile
        self.masked = text
        self.view = graph.view(path, 'html', 0, len(text))
        self.stack = []
        self.embedded_start = {}

    def source_offset(self):
        line, col = self.getpos()
        return self.graph.line_starts[self.path][line - 1] + col

    def handle_starttag(self, tag, attrs):
        if not re.fullmatch(self.profile.get('tag_name_pattern', r'.+'), tag):
            return
        start = self.source_offset()
        raw = self.get_starttag_text()
        attributes = dict(attrs)
        while self.stack and tag in self.profile.get('implicit_close_on_open', {}).get(self.stack[-1][0], []):
            self.stack.pop()
        parent = self.stack[-1][1] if self.stack else self.view
        ev = self.graph.evidence(self.path, start, start + len(raw))
        nid = self.graph.node(f'element:{self.path}:{start}', 'element', tag=tag, label=tag,
                              language='html', attributes=attributes, evidence=ev)
        self.graph.edge(parent, nid, 'contains', ev, 'observed')
        if tag == 'form':
            label = attributes.get('id') or attributes.get('name') or attributes.get('action') or f'form@{start}'
            self.graph.nodes[nid]['capability'] = self.graph.capability(self.path, label, nid, 'html')
        for rule in self.profile.get('attribute_rules', []):
            if rule.get('tag') and rule['tag'] != tag:
                continue
            value = attributes.get(rule['attribute'])
            if value is None:
                continue
            for item in value.split() if rule.get('split') else [value]:
                target = f'{rule["node_type"]}:html:{item}'
                # DOM IDs/classes are document-scoped, not globally unique.
                if rule['node_type'] in ('dom_id', 'css_class', 'form_field'):
                    target += ':' + self.path
                fields = {}
                if rule['relation'] == 'submits_to':
                    fields['method'] = (attributes.get('method') or 'GET').upper()
                    target += ':' + fields['method']
                self.graph.node(target, rule['node_type'], label=item, language='html', evidence=ev, **fields)
                self.graph.edge(nid, target, rule['relation'], ev, 'observed', rule=rule['id'])
        if tag not in self.profile.get('void_tags', []):
            self.stack.append((tag, nid))
        for embedded in self.profile.get('embedded', []):
            if embedded['tag'] != tag or attributes.get('src'):
                continue
            mime = (attributes.get('type') or '').lower()
            if mime not in embedded.get('allowed_types', ['']):
                continue
            self.embedded_start[nid] = (start + len(raw), embedded['language'])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if index is None:
            return
        for _, nid in self.stack[index:]:
            if nid in self.embedded_start:
                start, language = self.embedded_start.pop(nid)
                if language in self.graph.selected:
                    if self.graph.core['languages'][language]['backend'] != 'regex_balanced':
                        self.graph.diag('unsupported_embedded_backend', self.path, start, self.source_offset(), language=language)
                    else:
                        scan_regex(self.graph, self.path, language, start, self.source_offset(), nid, source_override=self.masked)
        del self.stack[index:]

    def finish(self):
        self.close()
        for nid, (start, language) in self.embedded_start.items():
            self.graph.diag('unclosed_embedded_element', self.path, start, len(self.masked), language=language)
        if self.stack:
            self.graph.diag('unclosed_html_elements', self.path, 0, 0, count=len(self.stack))


def scan_html(graph, path):
    profile = graph.core['languages']['html']
    text = graph.texts[path]
    for exclusion in profile.get('exclude_regions', []):
        lexical = graph.core['languages'][exclusion['lexical_language']].get('lexical', {})
        for span in delimited_regions(text, exclusion, lexical):
            a, b = span['full_start'], span['full_end']
            text = text[:a] + _blank(text[a:b]) + text[b:]
    graph.files[path]['analyses'].append({'language': 'html', 'backend': 'html', 'coverage': 'source_structure'})
    parser = _HTMLAdapter(graph, path, profile, text)
    parser.feed(text)
    parser.finish()


def _discover(root, extensions, excludes):
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink()
                         and not any(fnmatch.fnmatch(d, pattern) for pattern in excludes))
        for name in sorted(files):
            path = Path(directory, name)
            if path.is_symlink() or not any(name.lower().endswith(ext.lower()) for ext in extensions):
                continue
            if any(fnmatch.fnmatch(path.relative_to(root).as_posix(), p) for p in excludes):
                continue
            yield path


def extract_repository(root, languages='python', registry_path=None, capabilities=None,
                       excludes=DEFAULT_EXCLUDES, max_file_bytes=2_000_000,
                       revision=None, source_url=None):
    """Extract a deterministic shared graph. Does not write files or execute targets.

    `languages` accepts a comma-separated string or an iterable. `all` selects
    every registry profile. `capabilities` maps path:qualname or node ID to an
    explicit domain label. max_file_bytes applies before decoding every file.
    """
    core = load_language_core(registry_path)
    selected = [s.strip().lower() for s in languages.split(',')] if isinstance(languages, str) else list(languages)
    selected = sorted(set(core['languages'] if selected == ['all'] else selected))
    if not selected or any(s not in core['languages'] for s in selected):
        raise ValueError(f'Unknown/empty language selection {selected}; available: {sorted(core["languages"])}')
    if max_file_bytes < 1:
        raise ValueError('max_file_bytes must be positive')
    capabilities = capabilities or {}
    if not isinstance(capabilities, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in capabilities.items()):
        raise ValueError('Capabilities must map strings to strings')
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError(f'Root does not exist: {root}')
    graph = _EvidenceGraph(core, selected, capabilities)
    profiles = {key: core['languages'][key] for key in selected}
    extensions = sorted({ext for p in profiles.values() for ext in p['extensions']})
    python_paths = []
    if sum(p['backend'] == 'python_ast' for p in profiles.values()) > 1:
        raise ValueError('Select only one python_ast profile at a time')
    for path in sorted(_discover(root, extensions, excludes)):
        rel = path.relative_to(root).as_posix()
        relevant = [lang for lang, p in profiles.items() if any(rel.lower().endswith(e.lower()) for e in p['extensions'])]
        try:
            if path.stat().st_size > max_file_bytes:
                graph.files[rel] = {'status': 'skipped_size_limit', 'size_bytes': path.stat().st_size,
                                    'sha256': None, 'analyses': []}
                graph.diagnostics.append({'kind': 'size_limit', 'path': rel, 'limit': max_file_bytes})
                continue
            raw = path.read_bytes()
            if any(profiles[l]['backend'] == 'python_ast' for l in relevant):
                with tokenize.open(path) as handle:
                    text = handle.read()
            else:
                text = raw.decode('utf-8')
            graph.source(rel, raw, text)
        except (OSError, UnicodeError, SyntaxError, LookupError) as error:
            graph.files[rel] = {'status': 'unreadable', 'sha256': None, 'analyses': []}
            graph.diagnostics.append({'kind': 'source_read_error', 'path': rel, 'message': str(error)})
            continue
        for language in relevant:
            profile = profiles[language]
            if profile['backend'] == 'python_ast':
                python_paths.append(rel)
            elif profile['backend'] == 'html':
                if language != 'html':
                    raise ValueError('HTML backend profile must be named html')
                scan_html(graph, rel)
            else:
                region = profile.get('regions')
                if region:
                    spans = delimited_regions(text, region, profile.get('lexical', {}))
                    for span in spans:
                        scan_regex(graph, rel, language, span['start'], span['end'])
                    if not spans:
                        graph.files[rel]['analyses'].append({'language': language, 'coverage': 'no_regions', 'backend': 'regex_balanced'})
                        graph.diag('no_language_regions', rel, language=language)
                else:
                    scan_regex(graph, rel, language)
    if python_paths:
        profile = next(p for p in profiles.values() if p['backend'] == 'python_ast')
        # Explicit file list means the native adapter scans exactly the registry-selected files.
        native = PythonGraphExtractor(root, packages=python_paths, capabilities=capabilities,
            rules=profile.get('effects', {}), excludes=excludes,
            revision=revision, source_url=source_url, extensions=profile['extensions']).extract()
        for node in native['nodes']:
            if node['id'] == 'repository:.':
                continue
            node['language'] = 'python'
            if 'evidence' in node:
                node['evidence']['column_unit'] = 'utf8_byte'
            graph.nodes[node['id']] = node
        for edge in native['edges']:
            if 'evidence' in edge:
                edge['evidence']['column_unit'] = 'utf8_byte'
        graph.edges.extend(native['edges'])
        graph.diagnostics.extend(native['diagnostics'])
        for path, info in native['provenance']['files'].items():
            graph.files[path]['status'] = info['status']
            graph.files[path]['analyses'].append({'language': 'python', 'backend': 'python_ast',
                                                 'coverage': 'ast' if info['status'] == 'parsed' else 'unparseable'})
    # Rule fingerprint captures the complete effective library, including embedded/excluded profiles.
    source_facts = {p: {'sha256': info['sha256'], 'status': info['status']} for p, info in sorted(graph.files.items())}
    output = {'schema': MULTI_SCHEMA, 'generator': 'code_extract.py', 'languages': selected,
              'nodes': sorted(graph.nodes.values(), key=lambda n: n['id']),
              'edges': sorted({_json(e): e for e in graph.edges}.values(), key=lambda e: (e['from'], e['rel'], e['to'], _json(e))),
              'provenance': {'source_digest': _digest(_json(source_facts)), 'files': graph.files,
                             'revision': revision, 'source_url': source_url, 'license_status': 'not_assessed'},
              'config': {'rules': core, 'registry_sha256': _digest(_json(core)), 'languages': selected,
                         'excludes': list(excludes), 'max_file_bytes': max_file_bytes},
              'diagnostics': graph.diagnostics,
              'limitations': {lang: profiles[lang].get('limitations', []) for lang in selected}}
    output['capabilities'] = capability_shapes(output)
    output['summary'] = {'files': len(graph.files), 'nodes': len(output['nodes']), 'edges': len(output['edges']),
        'diagnostics': len(graph.diagnostics), 'failed_or_skipped_files': sum(i['status'] != 'parsed' for i in graph.files.values()),
        'by_language': {lang: sum(n.get('language') == lang for n in output['nodes']) for lang in selected}}
    validate_graph(output)
    return output


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ['--self-test']:
        return self_test()
    if argv and argv[0] in ('query', 'compare'):
        mode = argv.pop(0)
        parser = argparse.ArgumentParser(prog='code_extract.py ' + mode)
        if mode == 'query':
            parser.add_argument('graph')
            parser.add_argument('action', choices=['find', 'where', 'deps', 'who', 'blast', 'seam', 'path'])
            parser.add_argument('ref')
            parser.add_argument('other', nargs='?')
            parser.add_argument('--rel', action='append')
            parser.add_argument('--depth', type=int, default=1)
        else:
            parser.add_argument('target')
            parser.add_argument('peers', nargs='+')
            parser.add_argument('--threshold', type=float, default=.8)
            parser.add_argument('--min-peers', type=int, default=2)
            parser.add_argument('--allow-inferred', action='store_true')
        parser.add_argument('--out')
        args = parser.parse_args(argv)
        try:
            def read(path):
                return json.loads(Path(path).read_text(encoding='utf-8'))
            result = (query_graph(read(args.graph), args.action, args.ref, args.other, args.rel, args.depth)
                      if mode == 'query' else compare_graphs(read(args.target), [read(p) for p in args.peers],
                          args.threshold, args.min_peers, args.allow_inferred))
            if args.out:
                _write_json(args.out, result)
            else:
                print(_json(result), end='')
            return 0
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f'error: {error}', file=sys.stderr)
            return 2
    if argv and argv[0] == 'extract':
        argv.pop(0)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.')
    parser.add_argument('--language', default='python', help='Comma-separated language IDs, or all')
    parser.add_argument('--lang-core', help='Registry path; defaults beside this .py file')
    parser.add_argument('--out')
    parser.add_argument('--capabilities', help='JSON map of path:qualified_name to domain capability')
    parser.add_argument('--exclude', action='append', default=[])
    parser.add_argument('--max-file-bytes', type=int, default=2_000_000)
    parser.add_argument('--revision')
    parser.add_argument('--source-url')
    parser.add_argument('--list-languages', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.list_languages:
            core = load_language_core(args.lang_core)
            print(_json({lang: {'backend': profile['backend'], 'extensions': profile['extensions']}
                         for lang, profile in core['languages'].items()}), end='')
            return 0
        if not args.out and not args.dry_run:
            parser.error('--out is required unless --dry-run or --list-languages is used')
        capabilities = json.loads(Path(args.capabilities).read_text(encoding='utf-8')) if args.capabilities else None
        graph = extract_repository(args.root, args.language, args.lang_core, capabilities,
            (*DEFAULT_EXCLUDES, *args.exclude), args.max_file_bytes, args.revision, args.source_url)
        if not args.dry_run:
            _write_json(args.out, graph)
        print(_json(graph['summary']), end='')
        return 1 if graph['summary']['failed_or_skipped_files'] else 0
    except (OSError, ValueError, KeyError, TypeError, re.error) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


def self_test():
    """Run embedded Python and multilingual regression fixtures."""
    import tempfile
    import unittest
    c = dg = sys.modules[__name__]
    class GraphTests(unittest.TestCase):
    
        def graph(self, files, capabilities=None):
            with tempfile.TemporaryDirectory() as tmp:
                for p, content in files.items():
                    path = Path(tmp, p)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
                return dg.extract_python(tmp, capabilities=capabilities)
    
        def test_relative_and_alias(self):
            g = self.graph({'pkg/__init__.py': 'from .worker import run\ndef main(): return run()\n', 'pkg/worker.py': 'def run(): return 1\n', 'other.py': 'from pkg.worker import run as execute\ndef call(): return execute()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertEqual(len(calls), 2)
            self.assertTrue(all((e['to'] == 'symbol:pkg/worker.py:run' for e in calls)))
    
        def test_nested_scope(self):
            g = self.graph({'a.py': 'def x():\n def child():\n  dangerous()\n return 1\n'})
            self.assertFalse(any((e['rel'] == 'calls' and e['from'] == 'symbol:a.py:x' for e in g['edges'])))
            self.assertTrue(any((e['rel'] == 'calls' and e['from'] == 'symbol:a.py:x.child' for e in g['edges'])))
    
        def test_shadowing(self):
            g = self.graph({'a.py': 'def target(): pass\ndef f(target): return target()\ndef g():\n target = unknown\n return target()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertTrue(all((e['resolution'] == 'local_or_rebound' for e in calls)))
    
        def test_class_scope(self):
            g = self.graph({'a.py': 'class C:\n def helper(self): pass\n def go(self):\n  self.helper()\n  helper()\n'})
            calls = [e for e in g['edges'] if e['rel'] == 'calls']
            self.assertTrue(any((e['to'] == 'symbol:a.py:C.helper' for e in calls)))
            self.assertTrue(any((e['resolution'] == 'unresolved' for e in calls)))
    
        def test_contract(self):
            g = self.graph({'a.py': 'async def f(a:int, /, b=2, *args, c:str, d=None, **kw)->dict:\n return {"x":a}\n'})
            f = next((n for n in g['nodes'] if n['id'] == 'symbol:a.py:f'))
            p = f['contract']['parameters']
            self.assertEqual(len(p), 6)
            self.assertEqual(p[0]['kind'], 'positional_only')
            self.assertTrue(p[3]['required'])
            self.assertFalse(p[4]['required'])
            self.assertTrue(f['async'])
    
        def test_behavior(self):
            code = "@app.post('/orders/{order_id}')\nasync def create(order_id: int, payload):\n if not payload:\n  raise ValueError('bad')\n with db.transaction():\n  validate(payload)\n  await db.save(payload)\n try:\n  return payload['id']\n except KeyError:\n  return None\n"
            g = self.graph({'a.py': code})
            rels = {e['rel'] for e in g['edges']}
            self.assertTrue({'branches', 'raises', 'uses_context', 'awaits', 'exposes', 'handles', 'returns', 'reads'} <= rels)
            self.assertEqual(g['capabilities'][0]['key'], 'http:POST:/orders/{}')
            self.assertIn('tag:validation_candidate', g['capabilities'][0]['features'])
            self.assertTrue(dg.validate_graph(g))
    
        def test_parse_error(self):
            g = self.graph({'bad.py': 'def ???', 'good.py': 'def ok(): pass'})
            self.assertEqual(g['summary']['unparseable_files'], 1)
            self.assertEqual(g['summary']['parsed_files'], 1)
    
        def test_determinism(self):
            files = {'a.py': 'def f(x):\n if x: return x\n return 0\n'}
            self.assertEqual(dg._json(self.graph(files)), dg._json(self.graph(files)))
    
        def test_compare(self):
            mapping = {'a.py:save': 'order.create'}
            ours = self.graph({'a.py': 'def save(x): return x\n'}, mapping)
            p1 = self.graph({'a.py': 'def save(x):\n validate(x)\n return x\n'}, mapping)
            p2 = self.graph({'a.py': 'def save(x):\n validate(x)\n return x # different\n'}, mapping)
            result = dg.compare_graphs(ours, [p1, p2])
            hit = next((f for f in result['findings'] if f['feature'] == 'tag:validation_candidate'))
            self.assertEqual(hit['peer_support'], 2)
            self.assertEqual(hit['classification'], 'review_candidate')
            self.assertFalse(dg.compare_graphs(ours, [p1, p1])['findings'])
    
        def test_alignment_abstains(self):
            g = self.graph({'a.py': 'def f(): pass'})
            self.assertFalse(dg.compare_graphs(g, [g])['coverage'])
            g = self.graph({'a.py': 'def f(): pass\ndef h(): pass'}, {'a.py:f': 'same', 'a.py:h': 'same'})
            self.assertEqual(dg.compare_graphs(g, [])['ambiguous_target_capabilities'], ['same'])
    
        def test_query(self):
            g = self.graph({'a.py': 'def b(): pass\ndef a(): b()'})
            self.assertEqual(dg.query_graph(g, 'path', 'symbol:a.py:a', 'symbol:a.py:b', ['calls'])['path'], ['symbol:a.py:a', 'symbol:a.py:b'])
            self.assertEqual(dg.query_graph(g, 'who', 'symbol:a.py:b', rels=['calls'])['count'], 1)
    
        def test_duplicate_definitions(self):
            g = self.graph({'a.py': 'def a(): pass\ndef a(): pass\ndef f(): a()'})
            self.assertEqual(len([e for e in g['edges'] if e['rel'] == 'calls']), 2)
            dg.validate_graph(g)
    
        def test_no_execution(self):
            g = self.graph({'a.py': 'raise RuntimeError("must not execute")\ndef f(): pass'})
            self.assertEqual(g['summary']['parsed_files'], 1)
    
        def test_external_alias(self):
            g = self.graph({'a.py': 'import requests as r\ndef f(): return r.post("https://example.test")'})
            e = next((e for e in g['edges'] if e['rel'] == 'calls'))
            self.assertEqual(e['to'], 'external:requests.post')
            self.assertIn('network_candidate', e['tags'])
    
        def test_predicate_shape(self):
            self.assertEqual(dg.normalized_expression(dg.ast.parse('x > 0', mode='eval').body), dg.normalized_expression(dg.ast.parse('amount > 0', mode='eval').body))
            self.assertNotEqual(dg.normalized_expression(dg.ast.parse('x > 0', mode='eval').body), dg.normalized_expression(dg.ast.parse('x >= 0', mode='eval').body))
    
        def test_fields_and_parameter_edges(self):
            g = self.graph({'a.py': 'class Order:\n quantity: int = 0\ndef f(payload): return payload["id"]\n'})
            self.assertTrue(any((n['type'] == 'field' and n['annotation'] == 'int' for n in g['nodes'])))
            self.assertTrue(any((e['rel'] == 'references_parameter' for e in g['edges'])))
    class MultiTests(unittest.TestCase):
    
        def extract(self, files, languages='all', **kw):
            with tempfile.TemporaryDirectory() as tmp:
                for path, content in files.items():
                    p = Path(tmp, path)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(content if isinstance(content, bytes) else content.encode())
                return c.extract_repository(tmp, languages, **kw)
    
        def test_python_preserved(self):
            g = self.extract({'a.py': 'def f(x:int)->int:\n if x<0: raise ValueError()\n return x\n'}, 'python')
            f = next((n for n in g['nodes'] if n['id'] == 'symbol:a.py:f'))
            self.assertEqual(f['contract']['parameters'][0]['annotation'], 'int')
            self.assertTrue(any((e['rel'] == 'raises' for e in g['edges'])))
    
        def test_php_scopes(self):
            g = self.extract({'a.php': '<?php class Orders { function create($x, $y=1): int { if (!$x) { throw new Exception(); } validate($x); return $y; } } ?>'}, 'php')
            f = next((n for n in g['nodes'] if n.get('qualname') == 'Orders.create'))
            self.assertEqual(len(f['contract']['parameters']), 2)
            self.assertTrue(any((e['from'] == f['id'] and e['rel'] == 'calls' and ('validation_candidate' in e.get('tags', [])) for e in g['edges'])))
    
        def test_js(self):
            g = self.extract({'app.js': 'import x from "./x.js";\nasync function run(x) { await fetch("/orders"); return x; }\nconst save = (x) => { validate(x); };'}, 'javascript')
            self.assertEqual({n['label'] for n in g['nodes'] if n['type'] == 'function'}, {'run', 'save'})
            self.assertTrue(any((e['rel'] == 'imports' for e in g['edges'])))
            self.assertTrue(any((n['type'] == 'endpoint' and n['label'] == '/orders' for n in g['nodes'])))
    
        def test_ignore_comments_strings(self):
            g = self.extract({'a.js': '// function ghost() { bad(); }\nconst s="function fake() { bad(); }";\nfunction real(){ const x="}"; /* { */ good(); }'}, 'javascript')
            self.assertEqual([n['label'] for n in g['nodes'] if n['type'] == 'function'], ['real'])
            self.assertFalse(any((n.get('label') == 'bad' for n in g['nodes'])))
    
        def test_quoted_fake_reference(self):
            g = self.extract({'a.js': 'const text = "fetch(\'/fake\')"; fetch(\'/real\');'}, 'javascript')
            self.assertEqual([n['label'] for n in g['nodes'] if n['type'] == 'endpoint'], ['/real'])
    
        def test_css(self):
            g = self.extract({'a.css': '.card { color: red; background: url("/bg.png"); } @media screen { .card:hover { color: blue; } }'}, 'css')
            self.assertEqual(len([n for n in g['nodes'] if n['type'] == 'style_rule']), 3)
            self.assertTrue(any((n.get('kind') == 'declares_style' and n.get('name') == 'color' for n in g['nodes'])))
            self.assertTrue(any((n['type'] == 'resource' and n['label'] == '/bg.png' for n in g['nodes'])))
    
        def test_html(self):
            g = self.extract({'a.html': '<form action="/orders" method="post"><label for="qty">Q</label><input id="qty" name="qty" required><button>Go</button></form>'}, 'html')
            self.assertTrue(any((e['rel'] == 'submits_to' for e in g['edges'])))
            self.assertTrue(any((n['type'] == 'endpoint' and n.get('method') == 'POST' for n in g['nodes'])))
            self.assertEqual(len([n for n in g['nodes'] if n['type'] == 'element']), 4)
    
        def test_embedded(self):
            g = self.extract({'a.html': '<html>\n<script>function go(){ fetch("/x"); }</script>\n<style>.x { color:red; }</style></html>'}, 'html,javascript,css')
            f = next((n for n in g['nodes'] if n['type'] == 'function'))
            self.assertEqual(f['evidence']['line'], 2)
            self.assertTrue(any((n['type'] == 'style_rule' for n in g['nodes'])))
            self.assertFalse(any((n['type'] == 'function' for n in self.extract({'a.html': '<script>function go(){}</script>'}, 'html')['nodes'])))
    
        def test_nonjs_script(self):
            g = self.extract({'a.html': '<script type="application/json">{"function": "x"}</script>'}, 'html,javascript')
            self.assertFalse(any((n.get('language') == 'javascript' for n in g['nodes'])))
    
        def test_mixed_php(self):
            g = self.extract({'a.php': '<div><?php function run(){ echo "?>"; return 1; } ?></div><script>function click(){ run(); }</script>'})
            self.assertEqual({n.get('language') for n in g['nodes'] if n['type'] == 'function'}, {'php', 'javascript'})
            self.assertTrue(any((n['type'] == 'element' and n.get('tag') == 'div' for n in g['nodes'])))
    
        def test_extension_selection(self):
            g = self.extract({'a.php': '<?php function a(){}', 'b.js': 'function b(){}', 'c.py': 'def c(): pass'}, 'php')
            self.assertEqual(set(g['provenance']['files']), {'a.php'})
    
        def test_registry_extension_and_new_language(self):
            with tempfile.TemporaryDirectory() as tmp:
                core = c.load_language_core()
                core['languages']['toy'] = {'backend': 'regex_balanced', 'extensions': ['.toy'], 'lexical': {'brackets': [['{', '}']]}, 'rules': [{'id': 'toy.unit', 'action': 'scope', 'pattern': 'unit\\s+(?P<name>\\w+)\\s*(?P<body>\\{)', 'node_type': 'function', 'relation': 'contains', 'body': ['{', '}']}]}
                config = Path(tmp, 'core.json')
                config.write_text(json.dumps(core))
                Path(tmp, 'sample.toy').write_text('unit hello { }')
                g = c.extract_repository(tmp, 'toy', registry_path=config)
                self.assertTrue(any((n['type'] == 'function' and n['label'] == 'hello' for n in g['nodes'])))
    
        def test_invalid_language(self):
            with self.assertRaises(ValueError):
                self.extract({'a.py': ''}, 'unknown')
    
        def test_invalid_rule(self):
            with tempfile.TemporaryDirectory() as tmp:
                core = c.load_language_core()
                core['languages']['javascript']['rules'][0]['action'] = 'exec'
                p = Path(tmp, 'core.json')
                p.write_text(json.dumps(core))
                with self.assertRaises(ValueError):
                    c.load_language_core(p)
    
        def test_balance_diagnostic(self):
            g = self.extract({'a.js': 'function broken() { if (x) {'}, 'javascript')
            self.assertTrue(any((d['kind'] == 'unbalanced_delimiter' for d in g['diagnostics'])))
    
        def test_size_and_decode(self):
            g = self.extract({'a.js': 'x' * 100, 'b.js': b'\xff'}, 'javascript', max_file_bytes=50)
            self.assertEqual(g['summary']['failed_or_skipped_files'], 2)
    
        def test_deterministic(self):
            files = {'a.js': 'function run(x){return x;}', 'a.css': '.x{color:red}'}
            self.assertEqual(c._json(self.extract(files)), c._json(self.extract(files)))
    
        def test_edges_and_query(self):
            g = self.extract({'a.php': '<?php function go(){ validate(1); }'}, 'php')
            c.validate_graph(g)
            self.assertEqual(c.query_graph(g, 'find', 'go')['matches'], ['symbol:a.php:go'])
            self.assertEqual(c.query_graph(g, 'deps', 'symbol:a.php:go', rels=['calls'])['count'], 1)
    
        def test_compare(self):
            kw = {'languages': 'javascript', 'capabilities': {'a.js:save': 'order.create'}}
            a = self.extract({'a.js': 'function save(x){return x;}'}, **kw)
            b = self.extract({'a.js': 'function save(x){validate(x);return x;}'}, **kw)
            result = c.compare_graphs(a, [b], min_peers=1)
            self.assertTrue(any((f['feature'] == 'tag:validation_candidate' for f in result['findings'])))
    
        def test_python_bad_syntax(self):
            g = self.extract({'a.py': 'def !', 'b.py': 'def f(): pass'}, 'python')
            self.assertEqual(g['summary']['failed_or_skipped_files'], 1)
    
        def test_no_target_execution(self):
            g = self.extract({'a.py': 'raise RuntimeError("never execute")'}, 'python')
            self.assertEqual(g['summary']['failed_or_skipped_files'], 0)
    
        def test_php_inside_script_is_not_js(self):
            g = self.extract({'a.php': '<script><?php function php_only(){} ?> function js_only() {}</script>'})
            self.assertEqual({(n['label'], n['language']) for n in g['nodes'] if n['type'] == 'function'}, {('php_only', 'php'), ('js_only', 'javascript')})
    
        def test_parameter_formula(self):
            g = self.extract({'a.php': '<?php function f(int $x, $y = 2) { return $x; }'}, 'php')
            f = next((n for n in g['nodes'] if n['type'] == 'function'))
            self.assertEqual(f['contract']['parameters'][0]['annotation'], 'int')
            self.assertTrue(f['contract']['parameters'][0]['required'])
            self.assertFalse(f['contract']['parameters'][1]['required'])
    
        def test_python_extension_from_registry(self):
            with tempfile.TemporaryDirectory() as tmp:
                core = c.load_language_core()
                core['languages']['python']['extensions'].append('.pyw')
                p = Path(tmp, 'core.json')
                p.write_text(json.dumps(core))
                Path(tmp, 'app.pyw').write_text('def launch(): pass')
                g = c.extract_repository(tmp, 'python', registry_path=p)
                self.assertTrue(any((n['type'] == 'function' and n['label'] == 'launch' for n in g['nodes'])))
    suite = unittest.TestSuite([
        unittest.defaultTestLoader.loadTestsFromTestCase(GraphTests),
        unittest.defaultTestLoader.loadTestsFromTestCase(MultiTests),
    ])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
