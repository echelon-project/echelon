"""Focused unit test for echelon_engine.atoms.session_offer.

Surface under test: SessionOffer — records a direct session's episode as a NEUTRAL card
+ a PENDING offer, then applies the author's promote/decline consent. The honesty contract
is the point (no self-grade): a fresh offer is neutral/pending; promote applies ONE capped
consent bump (source='consent') and is idempotent; decline never mints and leaves the card to decay.

Isolation: a CardStore on a tmp_path/'core_v2.db' (no shared default db touched).
"""
from __future__ import annotations

import pytest

from echelon_engine.atoms.cards import CardStore
from echelon_engine.atoms.session_offer import SessionOffer, CONSENT_Q


@pytest.fixture
def store(tmp_path):
    return CardStore(tmp_path / "core_v2.db")


@pytest.fixture
def offer(store):
    return SessionOffer(store=store)


def test_record_episode_creates_neutral_pending_offer(offer, store):
    cid = offer.record_episode("sess-A", "designed the consent gate", ["echelon:a", "echelon:b"])
    assert cid, "a non-empty episode must produce a card id"
    pend = offer.pending()
    assert len(pend) == 1
    row = pend[0]
    assert row["card_id"] == cid
    assert row["session"] == "sess-A"
    assert row["summary"] == "designed the consent gate"
    assert row["refs"] == ["echelon:a", "echelon:b"]
    # A freshly-recorded episode moves NO weight of its own — its score is the card's birth
    # default (the cold-borrow warmth from Card, currently 100.0), NOT a self-graded bump.
    # The honesty point is that record_episode never calls reinforce; consent does that later.
    assert row["score"] == store.card(cid).score


def test_empty_coords_offers_nothing(offer):
    cid = offer.record_episode("sess-B", "loaded nothing", [])
    assert cid == ""
    assert offer.pending() == []


def test_promote_applies_one_capped_consent_bump(offer, store):
    cid = offer.record_episode("sess-C", "kept-by-consent episode", ["echelon:x"])
    before = store.card(cid).score
    res = offer.promote(cid)
    assert res["ok"] is True
    assert res["consent_q"] == CONSENT_Q
    after = store.card(cid).score
    # CONSENT_Q (62) > 50 -> a positive, MODEST lift off the neutral floor.
    assert after > before
    assert res["new_score"] == round(after, 1)
    # the offer leaves the pending queue once decided.
    assert offer.pending() == []


def test_promote_is_idempotent_no_double_credit(offer):
    cid = offer.record_episode("sess-D", "consent applied once", ["echelon:y"])
    first = offer.promote(cid)
    assert first["ok"] is True
    second = offer.promote(cid)
    assert second["ok"] is False
    assert "already promoted" in second["reason"]


def test_promote_unknown_offer_refused(offer):
    res = offer.promote("no-such-card-id")
    assert res["ok"] is False
    assert res["reason"] == "no such offer"


def test_decline_mints_nothing_and_dequeues(offer, store):
    cid = offer.record_episode("sess-E", "declined episode", ["echelon:z"])
    before = store.card(cid).score
    res = offer.decline(cid)
    assert res["ok"] is True
    after = store.card(cid).score
    # nothing minted: the neutral card is left exactly as-is (decays naturally, never deleted).
    assert after == before
    assert offer.pending() == []
    # the card still EXISTS (substrate law: never deleted).
    assert store.card(cid) is not None


def test_decline_then_promote_refused(offer):
    cid = offer.record_episode("sess-F", "declined then promote", ["echelon:w"])
    assert offer.decline(cid)["ok"] is True
    res = offer.promote(cid)
    assert res["ok"] is False
    assert "already declined" in res["reason"]


def test_pending_scoped_to_session(offer):
    offer.record_episode("sess-G", "g episode", ["echelon:g"])
    offer.record_episode("sess-H", "h episode", ["echelon:h"])
    g = offer.pending(session="sess-G")
    assert len(g) == 1 and g[0]["session"] == "sess-G"
