"""Content-addressing helpers — a pure leaf atom, no IO, no upward imports.

content_id: the sha256[:16] content-address that keys every UAME row.
domain_of: the COS top-level domain derived from a coordinate's first segment.

These are re-exported from uame.py for backward compat — callers import from .uame,
not from here directly.
"""
from __future__ import annotations
import hashlib
import json
import re

_DOMAIN_RE = re.compile(r"[^a-z0-9]+")


def content_id(content: str, domain: str, kind: str) -> str:
    raw = json.dumps({"content": content, "domain": domain, "kind": kind}, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def domain_of(coordinate: str, fallback: str = "general") -> str:
    """The COS top level = the table. First segment of the coordinate, sanitized to a safe
    table-name token. Empty coordinate -> fallback domain (e.g. 'general' or a raw bucket)."""
    head = (coordinate or "").split(":", 1)[0].strip().lower()
    head = _DOMAIN_RE.sub("_", head).strip("_")
    return head or fallback
