"""Scope-to-domain routing — a pure leaf atom, no IO, no DB.

scope_to_domain maps a (scope, coordinate) pair to a COS domain (the UAME table suffix).
Coordinate wins when present. Unmapped scopes fall back to address sanitization.

Re-exported from store.py for backward compat.
"""
from __future__ import annotations

from .addressing import domain_of as _domain_of

# scope -> COS domain map (shared with the migration). A scope is a project/purpose tag; the
# domain is the COS top level = the table. Unmapped scopes derive a domain from a coordinate head
# (if any) or fall back to a sanitized form of the scope itself.
#: Only generic, engine-owned scopes belong here. A project scope needs no row:
#: the fallback below derives its domain from the scope name itself.
_SCOPE_DOMAIN = {
    "echelon-self": "identity", "reclaim-research": "reclaim",
    "reclaim-test": "reclaim", "claude-self": "claude", "claude-self-raw": "claude",
    "synthesis-proposals": "synthesis",
}


def scope_to_domain(scope: str, coordinate: str = "") -> str:
    """Map to a COS domain (the table). COORDINATE WINS when present — that is the whole point of
    domain-tables: an atom coordinated `tooling:bash:gzip` belongs in `*_tooling`, regardless of
    which scope captured it. Else the explicit scope map; else test-ish scopes; else sanitized
    scope. NOTE: because a write may have a coordinate and a later reinforce()/lookup may not,
    routing for the SAME seed can differ — so all id-based lookups (_row) MUST fall back to an
    all-tables search by id. The id is the real key (content-addressed PK); the domain is just
    which table it sits in."""
    if coordinate:
        return _domain_of(coordinate)
    if scope in _SCOPE_DOMAIN:
        return _SCOPE_DOMAIN[scope]
    if scope.startswith(("test-", "grow-", "wire-", "decay-", "promo", "tune-", "synth", "final-")):
        return "test"
    return _domain_of(scope) if scope else "general"
