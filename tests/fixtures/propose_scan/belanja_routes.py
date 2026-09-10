def _board(request: Request):
    """The live board off the app module — never a constant, never a mock.
    Same discipline as buku_stok_routes._board / produk_routes._board."""
    from os_client import main as _main

    return _main._BOARD


def _unwrap(result):
    """Board reads come back enveloped (`BoardBase.respond`). Take the
    payload. Same discipline as buku_stok_routes._unwrap."""
    if isinstance(result, dict) and "data" in result and "meta" in result:
        data = result.get("data")
        if isinstance(data, list):
            return data[0] if len(data) == 1 else data
        return data
    return result


def _receipt(changed: str, seam: str, pre_read: Callable[[], object]) -> dict:
    """THE BORN-HERE RECEIPT HELPER, COPIED VERBATIM (BLUEPRINT "INHERITED
    MACHINERY: the pre-read receipt helper... COPY the pattern verbatim into
    belanja_routes.py; do not regress to Produk's literal-`old` form").

    `old` is never accepted as a literal argument here — the only way to
    supply it is `pre_read`, a zero-arg callable this function invokes
    itself, synchronously, before returning.

    `new` is DELIBERATELY NOT a parameter of this helper — the caller reads
    `old` through `_receipt`, performs the actual write, and builds the
    final `{changed, old, new, seam}` dict itself with the write's own
    result as `new`.

    Returns `{"changed": changed, "old": <pre_read()>, "seam": seam}` — the
    caller merges in `"new"` after the write.
    """
    if not callable(pre_read):
        raise TypeError(
            "_receipt(changed, seam, pre_read) requires pre_read to be a "
            "zero-arg CALLABLE — old is fetched by calling it, never passed "
            "as a literal (BLUEPRINT 'THE BORN-HERE DEBT': this is the "
            "fabricated-old defect class dying by construction)"
        )
    old = pre_read()
    return {"changed": changed, "old": old, "seam": seam}


@router.get("/api/belanja/overview")
def belanja_overview(request: Request, cover_days: int | None = None,
                     lead_days: int | None = None):
    """Saran's own floor: settings-recomputed reorder list + totals +
    multiplier-review counts, in ONE call (BLUEPRINT: "no per-row
    waterfall"). The Supplier tab is its OWN lazy GET
    (`/api/belanja/supplier/list`) — a registry the owner opens
    deliberately, not part of Saran's boot cost."""
    board = _board(request)
    cd = cover_days or _DEFAULT_COVER_DAYS
    ld = lead_days or _DEFAULT_LEAD_DAYS

    reorder, reorder_err = _safe(
        "reorder-list",
        lambda: _unwrap(board.dashboard_stock(
            "reorder-list", cover_days=cd, lead_days=ld)))
    mults, mults_err = _safe(
        "reorder-list-mult",
        lambda: _unwrap(board.dashboard_stock("reorder-list-mult")))

    errors: dict[str, str] = {}
    if reorder_err:
        errors["reorder-list"] = reorder_err
    if mults_err:
        errors["reorder-list-mult"] = mults_err

    raw_items = (reorder or {}).get("items", []) if reorder else []
    # F3 heal (Opus round-1): the honesty label (belum_terukur/is_spekulasi/
    # rate_label) is computed HERE, once, server-side — belanja.js renders
    # the flag, it no longer re-derives it. The Rp total is likewise
    # recomputed HERE so it can see the spekulasi flag the seam's own
    # total_rp_estimate cannot (AMB-1: spekulasi excluded from COMPUTED
    # TOTALS only, never from the row list itself).
    items = _annotate_honesty(raw_items)
    priced = sum(1 for it in items if it.get("price_thb"))
    unpriced = len(items) - priced

    return {
        "cover_days": cd,
        "lead_days": ld,
        "items": items,
        "total_items": len(items),
        "total_rp_estimate": _honest_rp_total(items),
        "rate": (reorder or {}).get("rate"),
        "priced_count": priced,
        "unpriced_count": unpriced,
        "products": (reorder or {}).get("products") or {},
        "multiplier_summary": {
            "set_count": (mults or {}).get("set_count"),
            "total": (mults or {}).get("total"),
        } if mults else None,
        "disabled": DISABLED_CAPABILITIES,
        "errors": errors,
        "degraded": bool(errors),
    }
