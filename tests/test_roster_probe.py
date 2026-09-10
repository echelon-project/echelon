"""R-0171 unit D — the `roster probe` verb and the seat-health skip it feeds.

The defect these tests pin (witnessed 2026-09-09, council trace des-vs-ims-anchor-0909):
`routing.model_chain` dropped a `:free` id only when the daily roster snapshot no longer
LISTED it, and never sent a token. So a chain kept seats that failed in 2 ms (no provider
wired), hung 302 s (bare id, no per-attempt timeout), or answered in the wrong shape
(reasoning leaked into `content`, cut at the word cap). Five seats cost 408 s; two answered.

Listing is a CATALOGUE fact; answering is a LIVE fact. These tests hold the line on both
halves of the fix: the probe records what a seat actually did, and the chain skips on it —
degrading OPEN (missing/stale health drops nothing) so the fix can never be worse than the
bug it replaces.

Every probe here rides an injected `send`, so the suite is $0 and offline.
"""
from __future__ import annotations

import json
import time

import pytest

from echelon_engine.atoms import roster, routing


class FakeResp:
    """The parts of LLMResponse the probe reads."""

    def __init__(self, content="OK", status="success", tokens_out=1, raw=None):
        self.content = content
        self.status = status
        self.tokens_out = tokens_out
        self.raw = raw


@pytest.fixture
def health_file(tmp_path, monkeypatch):
    """Point seat-health at a temp file so a test never reads or writes the real bank."""
    p = tmp_path / "seat-health.json"
    monkeypatch.setattr(roster, "HEALTH_PATH", p)
    return p


def _write_health(path, seats, age_s=0):
    """Seed a seat-health snapshot `age_s` seconds old."""
    ts = int(time.time()) - age_s
    for rec in seats.values():
        rec.setdefault("probed_at", ts)
    path.write_text(json.dumps({"ts": ts, "seats": seats, "ok": True}), encoding="utf-8")


def _no_roster(monkeypatch):
    """Silence the ROSTER filter so a test isolates the SEAT-HEALTH one."""
    monkeypatch.setattr(roster, "is_fresh", lambda snapshot=None: False)


# --------------------------------------------------------------------------
# DONE 2 — the chain skips a dead seat, and only when it has a fresh opinion
# --------------------------------------------------------------------------

def test_chain_drops_seat_a_fresh_probe_says_is_dead(health_file, monkeypatch):
    """The headline: a seat the probe found DEAD falls out, and the chain falls
    through to the next floor — the same shape of skip as a retired ':free' id."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {
        "council": {"pick": "dead-seat", "fallbacks": ["live-seat", "deepseek-chat"]},
    })
    _write_health(health_file, {
        "dead-seat": {"answered": False, "shape": "error", "error": "HTTP 429", "latency_ms": 2},
        "live-seat": {"answered": True, "shape": "ok", "error": "", "latency_ms": 900},
    })

    chain = routing.model_chain("council")

    assert "dead-seat" not in chain
    assert chain == ["live-seat", "deepseek-chat"]


def test_chain_drops_a_seat_that_answered_in_the_wrong_shape(health_file, monkeypatch):
    """A seat can return HTTP 200 and still be useless: the two nemotron seats in the
    trace ANSWERED and leaked their reasoning. answered=true is not enough — shape must
    be ok too."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {
        "council": {"pick": "leaky-seat", "fallbacks": ["live-seat"]},
    })
    _write_health(health_file, {
        "leaky-seat": {"answered": True, "shape": "leaked-reasoning", "error": ""},
        "live-seat": {"answered": True, "shape": "ok", "error": ""},
    })

    assert routing.model_chain("council") == ["live-seat"]


def test_stale_health_drops_nothing(health_file, monkeypatch):
    """Freshness is 1 h. An older file has NO opinion — the chain is untouched, which is
    exactly today's behaviour. Degrading OPEN is why this fix can't be worse than the bug."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {
        "council": {"pick": "dead-seat", "fallbacks": ["live-seat"]},
    })
    _write_health(health_file, {
        "dead-seat": {"answered": False, "shape": "error", "error": "HTTP 429"},
    }, age_s=roster.HEALTH_FRESH_SECONDS + 60)

    assert routing.model_chain("council") == ["dead-seat", "live-seat"]


def test_missing_health_file_drops_nothing(health_file, monkeypatch):
    """No probe has ever run: no opinion, no skip, no crash."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {
        "council": {"pick": "a", "fallbacks": ["b"]},
    })
    assert not health_file.exists()

    assert routing.model_chain("council") == ["a", "b"]


def test_an_all_dead_chain_is_refused_not_emptied(health_file, monkeypatch):
    """A skip that would empty the chain is refused: the original chain beats a dead one.
    A caller with a bad seat can still try; a caller with NO seat cannot."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {"council": {"pick": "a", "fallbacks": ["b"]}})
    _write_health(health_file, {
        "a": {"answered": False, "shape": "error", "error": "x"},
        "b": {"answered": False, "shape": "error", "error": "y"},
    })

    assert routing.model_chain("council") == ["a", "b"]


def test_corrupt_health_file_does_not_crash_the_chain(health_file, monkeypatch):
    """Routing is on the hot path of every call; a garbled weather file must never be
    able to take it down."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {"council": {"pick": "a", "fallbacks": ["b"]}})
    health_file.write_text("{not json", encoding="utf-8")

    assert routing.model_chain("council") == ["a", "b"]


def test_reason_for_names_the_skipped_seat(health_file, monkeypatch):
    """A silent skip is the failure the trace punished — the caller must be able to read
    WHY the chain got shorter, from the same door."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {
        "council": {"pick": "dead-seat", "fallbacks": ["live-seat"], "why": "declared floor"},
    })
    _write_health(health_file, {
        "dead-seat": {"answered": False, "shape": "error", "error": "HTTP 429 rate limited"},
        "live-seat": {"answered": True, "shape": "ok", "error": ""},
    })

    reason = routing.reason_for("council")

    assert "declared floor" in reason
    assert "dead-seat" in reason
    assert "429" in reason


def test_reason_for_is_unchanged_when_nothing_is_skipped(health_file, monkeypatch):
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {"council": {"pick": "a", "why": "declared floor"}})

    assert routing.reason_for("council") == "declared floor"


# --------------------------------------------------------------------------
# DONE 1 — the probe: timeout, shape classification, never crashing on one seat
# --------------------------------------------------------------------------

def test_probe_records_a_hang_as_a_timeout_not_a_302_second_wait(health_file):
    """The kimi-k3 failure: a bare id with no per-attempt timeout hung 302 s. The probe
    caps every attempt and records the cut, so the hang costs the CHAIN nothing."""
    def hanging_send(model_id):
        raise TimeoutError("timed out")

    snap = roster.probe(["moonshotai/kimi-k3"], timeout_s=0.05, send=hanging_send)
    rec = snap["seats"]["moonshotai/kimi-k3"]

    assert rec["answered"] is False
    assert "TimeoutError" in rec["error"]
    assert roster.is_dead("moonshotai/kimi-k3", snap)


def test_probe_classifies_a_leaked_reasoning_reply(health_file):
    """The nemotron failure: chain-of-thought lands in `content` where the answer belongs.
    HTTP 200, answered=true — and still unusable, so shape must catch it."""
    leak = "Okay, the user wants one word. Let me think about what to say here..."

    snap = roster.probe(["nemotron:free"], send=lambda m: FakeResp(content=leak))
    rec = snap["seats"]["nemotron:free"]

    assert rec["answered"] is True
    assert rec["shape"] == "leaked-reasoning"
    assert roster.is_dead("nemotron:free", snap), "a leaked-reasoning seat is not usable"


def test_probe_classifies_a_clean_answer_as_ok(health_file):
    snap = roster.probe(["good:free"], send=lambda m: FakeResp(content="OK"))
    rec = snap["seats"]["good:free"]

    assert (rec["answered"], rec["shape"]) == (True, "ok")
    assert not roster.is_dead("good:free", snap)
    assert isinstance(rec["latency_ms"], int)
    assert rec["probed_at"] >= snap["ts"] - 5


def test_probe_classifies_empty_and_truncated(health_file):
    """The other two shapes the trace showed: nothing back, and cut at the cap."""
    cut = FakeResp(content="Well, to answer that I would first", tokens_out=16,
                   raw={"choices": [{"finish_reason": "length"}]})

    snap = roster.probe(["empty:free"], send=lambda m: FakeResp(content="   "))
    assert snap["seats"]["empty:free"]["shape"] == "empty"

    snap = roster.probe(["cut:free"], send=lambda m: cut)
    assert snap["seats"]["cut:free"]["shape"] == "truncated"


def test_probe_records_an_error_status_response(health_file):
    """The gpt-5.4-mini failure: no provider wired, error in 2 ms. status != success is
    a dead seat even though nothing raised."""
    err = FakeResp(content="HTTP 404: no such model", status="error")

    rec = roster.probe(["gpt-5.4-mini"], send=lambda m: err)["seats"]["gpt-5.4-mini"]

    assert rec["answered"] is False
    assert "404" in rec["error"]


def test_one_bad_seat_never_kills_the_probe(health_file):
    """Five seats, one explodes: the other four must still be reported. A probe that dies
    on its first bad seat would be useless for exactly the chain that needs it."""
    def send(model_id):
        if model_id == "boom":
            raise RuntimeError("provider exploded")
        return FakeResp(content="OK")

    snap = roster.probe(["a", "boom", "b", "c"], send=send)

    assert set(snap["seats"]) == {"a", "boom", "b", "c"}
    assert snap["seats"]["boom"]["answered"] is False
    assert all(snap["seats"][m]["answered"] for m in ("a", "b", "c"))


def test_probe_never_writes_a_credential_into_the_health_file(health_file):
    """Error bodies are provider-controlled text. An excerpt is bounded AND key-shaped
    strings are redacted — the file is written to disk and read by other tools."""
    leaky = "Unauthorized: Bearer sk-NOTAREALKEY000000000000 rejected " + "x" * 500

    rec = roster.probe(["k"], send=lambda m: FakeResp(content=leaky, status="error"))["seats"]["k"]

    assert "sk-NOTAREALKEY000000000000" not in rec["error"]
    assert "sk-NOTAREALKEY000000000000" not in health_file.read_text(encoding="utf-8")
    assert len(rec["error"]) <= roster._ERROR_EXCERPT


def test_probe_merges_and_does_not_blank_other_roles_seats(health_file):
    """Probing one role's chain must not wipe another role's health — each seat carries
    its own probed_at, so freshness is per seat."""
    roster.probe(["seat-a"], send=lambda m: FakeResp(content="OK"))
    roster.probe(["seat-b"], send=lambda m: FakeResp(content="OK"))

    seats = roster.load_health()["seats"]
    assert set(seats) == {"seat-a", "seat-b"}


def test_probe_of_a_role_targets_the_declared_chain(monkeypatch):
    """--role probes every seat the TABLE declares for the role."""
    monkeypatch.setattr(routing, "_ROLES", {"council": {"pick": "a", "fallbacks": ["b"]}})

    assert roster.chain_ids("council") == ["a", "b"]


def test_probe_of_a_role_still_targets_a_seat_a_previous_probe_killed(health_file, monkeypatch):
    """THE RATCHET TRAP: probing model_chain() would target the chain a PREVIOUS probe
    already filtered, so a seat marked dead could never be re-probed and never come back.
    These seats die on WEATHER (rate limits, capacity) which clears within the hour — the
    probe must be able to resurrect a seat, not only bury it."""
    _no_roster(monkeypatch)
    monkeypatch.setattr(routing, "_ROLES", {"council": {"pick": "dead-seat", "fallbacks": ["live-seat"]}})
    _write_health(health_file, {
        "dead-seat": {"answered": False, "shape": "error", "error": "HTTP 429"},
        "live-seat": {"answered": True, "shape": "ok", "error": ""},
    })
    assert routing.model_chain("council") == ["live-seat"], "precondition: the seat is filtered out"

    assert "dead-seat" in roster.chain_ids("council")


def test_chain_ids_of_an_unknown_role_is_the_driver_chain(monkeypatch):
    """Unknown role falls to the driver chain (routing's standing rule) rather than
    handing the probe an empty target set."""
    monkeypatch.setattr(routing, "_ROLES", {"driver": {"pick": "d", "fallbacks": ["e"]}})

    assert roster.chain_ids("nonexistent-role") == ["d", "e"]


# --------------------------------------------------------------------------
# The CLI door — `echelon roster probe`
# --------------------------------------------------------------------------

def test_probe_subcommand_help_is_reachable():
    """`roster probe --help` must exit 0. The bare `roster` verb has no argparse, so the
    subcommand's own parser is the only door to its flags."""
    with pytest.raises(SystemExit) as e:
        roster.main(["probe", "--help"])
    assert e.value.code == 0


def test_bare_roster_verb_still_refreshes(monkeypatch):
    """The probe subcommand must not shadow the daily refresh that rides `backup --push`."""
    monkeypatch.setattr(roster, "refresh", lambda: {"ok": True, "count": 3, "free": [],
                                                    "arrived": [], "retired": [],
                                                    "retired_routing_picks": []})
    assert roster.main([]) == 0


def test_probe_subcommand_with_no_target_is_an_error_not_a_silent_pass(capsys):
    """Neither --role nor --ids: refuse loudly. A probe that silently probes nothing would
    write an empty health file and look like a clean bill of health."""
    assert roster.main(["probe"]) == 2
    assert "--role" in capsys.readouterr().out


def test_probe_subcommand_reports_each_seat(health_file, monkeypatch, capsys):
    """The operator-facing half: every probed seat is named with its verdict, and the
    usable subset is summarised."""
    monkeypatch.setattr(roster, "probe", lambda ids, timeout_s=20.0: {
        "ts": int(time.time()), "ok": True,
        "seats": {"dead": {"answered": False, "shape": "error", "error": "HTTP 429",
                           "latency_ms": 2, "probed_at": int(time.time())},
                  "live": {"answered": True, "shape": "ok", "error": "",
                           "latency_ms": 800, "probed_at": int(time.time())}},
    })

    assert roster.main(["probe", "--ids", "dead,live"]) == 0

    out = capsys.readouterr().out
    assert "DEAD dead" in out and "HTTP 429" in out
    assert "OK  live" in out
    assert "answered-and-usable: 1/2" in out


def test_a_leak_that_is_also_truncated_is_named_a_leak(health_file):
    """CAPTURED LIVE 2026-09-09 from nvidia/nemotron-3-super-120b-a12b:free at the 16-token
    probe cap. Both verdicts are 'dead', but 'truncated' reads as 'cap too small' while the
    real fault is that the seat spends the answer slot on its reasoning — a bigger cap would
    not fix it. The leak check must therefore win over the length check."""
    live = FakeResp(content='The user says: "Reply with exactly one word: OK". So we must',
                    tokens_out=16, raw={"choices": [{"finish_reason": "length"}]})

    assert roster.classify_shape(live) == "leaked-reasoning"


def test_a_seat_that_returns_nothing_at_the_cap_is_empty_not_truncated(health_file):
    """CAPTURED LIVE 2026-09-09 from moonshotai/kimi-k3: finish_reason=length, tokens_out=16,
    and content ''. It burned the whole cap without emitting a visible token."""
    live = FakeResp(content="", tokens_out=16, raw={"choices": [{"finish_reason": "length"}]})

    assert roster.classify_shape(live) == "empty"
