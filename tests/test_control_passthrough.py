"""The steer-channel seam (framework design 15.4, scouted 2026-08-19): the
loop has honored control() per step since the partner-control build, but
dispatch never forwarded a key to that door. These tests pin the pass-through
chain dispatch -> _make_partner_runner -> make_agent_runner -> loop.run at $0
(loop.run monkeypatched; no network)."""
from __future__ import annotations

import pytest

pytest.importorskip("echelon_engine.agent.workflow",
                    reason="echelon_engine.agent.workflow not available")


class _FakeResult:
    status = "completed"
    answer = "done"
    steps = 1


def test_make_agent_runner_forwards_control(monkeypatch, tmp_path):
    import echelon_engine.agent.loop as loop_mod
    from echelon_engine.agent.workflow import make_agent_runner

    seen = {}

    def fake_run(goal, provider, tools, model_id=None, **kwargs):
        seen.update(kwargs)
        return _FakeResult()

    monkeypatch.setattr(loop_mod, "run", fake_run)

    sentinel = lambda: {"interject": "steer me"}  # noqa: E731
    run_step = make_agent_runner(object(), "fake-model", str(tmp_path),
                                 control=sentinel)
    out = run_step({"agent": "dev", "task": "noop"}, {})
    assert out["status"] == "completed"
    assert seen.get("control") is sentinel


def test_dispatch_signature_carries_control():
    """dispatch() exposes control and hands it to the runner chain — the
    kwarg exists at the ONE confirmed seam to a real loop."""
    import inspect
    from echelon_engine.agent import partner, workflow

    for fn in (partner.dispatch, partner._make_partner_runner,
               workflow.make_agent_runner):
        assert "control" in inspect.signature(fn).parameters, fn.__name__
