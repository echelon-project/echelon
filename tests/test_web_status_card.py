"""Tests for echelon_engine.agent.web.status_card — the pure render seams.

The web command-center is ported as an evolving app surface (agent-layer until a
services facade is built). These tests cover the PURE, no-I/O leaves: _banner
state mapping and render_html (card dict -> HTML). build_card touches the real
soul home, so it is NOT exercised here (we never write the live ~/.echelon).
"""
from __future__ import annotations

from echelon_engine.agent.web.status_card import render_html, _banner


def _card(**over) -> dict:
    base = {
        "title": "ECHELON Substrate", "feeling": "calm", "banner": "Idle",
        "state": "idle", "step": 0, "uptime": "1h", "swarms": 2, "agents": 4,
        "tasks": 7, "soul": "core_v2.db", "tokens": 12345, "usd": "0.42",
        "seeds_found": 3, "budget_status": "GREEN", "dream": None,
    }
    base.update(over)
    return base


def test_banner_known_states():
    assert "Cold" in _banner("cold")
    assert "Dreaming" in _banner("dreaming")
    assert "Awake" in _banner("running")
    assert "Idle" in _banner("idle")

def test_banner_unknown_state_falls_back():
    assert "Idle" in _banner("zzz")


def test_render_html_is_complete_document():
    html = render_html(_card())
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")

def test_render_html_includes_card_values():
    html = render_html(_card(title="My Run", state="running", step=12))
    assert "My Run" in html
    assert "running" in html
    assert ">12<" in html  # step rendered

def test_render_html_token_thousands_separator():
    html = render_html(_card(tokens=1234567))
    assert "1,234,567" in html

def test_render_html_no_dream_message():
    html = render_html(_card(dream=None))
    assert "no dream yet" in html

def test_render_html_with_dream_counts():
    html = render_html(_card(dream={
        "proposals": 5,
        "nominated": ["a", "b"],
        "self_seeded": ["c"],
    }))
    assert "Proposals: 5" in html
    assert "Witnessed (rising): 2" in html
    assert "Wished (own core): 1" in html

def test_render_html_budget_status_class_lowercased():
    html = render_html(_card(budget_status="AMBER"))
    assert 'class="amber"' in html
    assert "AMBER" in html
