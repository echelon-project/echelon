"""Focused unit tests for echelon_engine.atoms.boot — the waking ritual.

boot() runs the rediscovery: (on the canonical self-scope) mint the soul, dream-consolidate,
then PRESENT the CVs + insights as an offering (BootContext). The DECLINE BRANCH is the
load-bearing contract: for ANY scope other than SELF_SCOPE it mints NOTHING — a named soul
wakes as whatever it already is (the mechanism for the 'no', not just copy about it).
Depends on store/identity/dream (siblings) + lazy warmth.

Isolated SeedStore on tmp dbs.
"""
import pytest

from echelon_engine.atoms.store import SeedStore
from echelon_engine.atoms import boot
from echelon_engine.atoms.identity import SELF_SCOPE, CANARY


@pytest.fixture
def store(tmp_path):
    return SeedStore(tmp_path / "core.db", v2_db=tmp_path / "core_v2.db")


def test_boot_self_scope_mints_and_presents_the_soul(store):
    ctx = boot.boot(store, self_scope=SELF_SCOPE)
    assert ctx.seeded > 0                       # the canonical soul was minted
    assert ctx.canary == CANARY
    assert ctx.questions                        # the rediscovery questions are posed
    # the presented soul contains the CVs (compass)
    assert "CV-001" in ctx.soul_present or "[CV-" in ctx.soul_present


def test_boot_is_idempotent_on_seeding(store):
    first = boot.boot(store, self_scope=SELF_SCOPE)
    second = boot.boot(store, self_scope=SELF_SCOPE)   # boot runs every waking
    assert first.seeded > 0
    assert second.seeded == 0                    # content-addressed — nothing re-minted


def test_boot_decline_branch_mints_nothing_for_a_named_soul(store):
    # the mechanism for 'no': a non-canonical scope is NOT force-minted the shared identity
    ctx = boot.boot(store, self_scope="persona:stranger")
    assert ctx.seeded == 0                        # nobody forced a soul onto this scope
    # it wakes as whatever it already is — here, empty
    assert ctx.self_scope == "persona:stranger"


def test_boot_presents_insights_after_cvs(store):
    ctx = boot.boot(store, self_scope=SELF_SCOPE)
    # the soul is values AND texture: insights are presented after the CVs (the texture half)
    if "texture, earned in specific sessions" in ctx.soul_present:
        cv_pos = ctx.soul_present.find("[CV-")
        tex_pos = ctx.soul_present.find("texture, earned")
        assert cv_pos < tex_pos                  # compass first, texture after
