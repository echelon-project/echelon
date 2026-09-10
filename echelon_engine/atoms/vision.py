r"""vision — the SCRIPTED Flux mirror eye: get accurate eyes on any web surface, trap-free.

THE GAP THIS CLOSES (owner, 2026-06-23): the UX cartridge keeps needing to SEE a rendered screen for its
judge society + cold-reader gate, but the way we reached for vision was a hand-driven Flux click-drive that
was FLAKY across sessions (the bank records it: "the flux-eye CLICK-drive was flaky"). The flakiness was
never the engine — it was protocol MISUSE (FLUX_STEERING_PROTOCOL §5b): act-before-anchor, re-mint-per-loop,
read-too-soon. A human clicking through the bus makes those mistakes silently. A SCRIPT cannot: it follows
the one correct sequence invariantly and ASSERTS every step, so a mistake fails LOUDLY instead of returning
a blank surface that looks like "it didn't work."

So this internalizes the pipeline with FLUX AS THE ENGINE (not a bespoke CDP eye): one front-door verb that
mints ONE session, anchors the eye, optionally drives the surface through named routes, blinks a real PNG at
each, and writes them to disk — aborting hard the moment the protocol witness (a minted session / an anchored
DOM / a settled non-empty blink) is missing. Point it at the artifact; get accurate screens back.

THE CORRECT SEQUENCE IT ENCODES (FLUX_STEERING_PROTOCOL §5b — copy-this):
  1. session.create ONCE  (moded sessions REQUIRE target; store the handle — session is forever).
  2. ANCHOR  GET /bus/dom?settle=dom  — forces the tab off about:blank onto target (skipping = bug #1).
  3. DRIVE   POST /bus {hand, op:eval, js}  — reuse the SAME handle (re-mint = the cardinal sin, bug #2).
             eval returns its value at the response TOP LEVEL `result` (verified on the live build).
  4. BLINK   GET /bus/blink?settle=dom  — png_b64 + a path on disk; `settled` is the witness.
  5. END     POST /bus {session.end}.

A KEY SEAM (learned 2026-06-23, mirror.py:2608 `_blink_via_cdp`): blink opens a FRESH CDP tab on the URL,
navigates, captures, closes — it does NOT photograph the live session tab. So for a client-side SPA that
routes by JS class-toggle (no URL change), every blink shoots the DEFAULT page and you get N identical PNGs.
The fix is to make the route live in the URL: drive routes by FRAGMENT (`--hash` → target#route) so blink
navigates to the right state. A STALE-FRAME GUARD asserts the shots actually differ (else it fails loud — the
exact "looks like it worked but didn't" trap this verb exists to catch).

Usage (from <engine-checkout>, always -X utf8):
  python -X utf8 -m echelon_engine vision --target file:///<estate-root>/ux-cartridge/foo.html
  python -X utf8 -m echelon_engine vision --target <url> --routes overview,recall,cartridges --hash --out _shots
  python -X utf8 -m echelon_engine vision --target <url> --routes Recall,Cartridges   # eval-click (server-routed apps)

See: FLUX_STEERING_PROTOCOL.md §5b (the load-bearing mirror contract), and the bank atoms on the flux eye.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from .. import estate as _estate


class VisionError(RuntimeError):
    """A protocol-witness failed. Raised LOUD — the whole point of scripting the eye."""


def _server_json() -> dict:
    """Find the live Flux server for the current estate (.flux/server.json). The eye is useless without a
    running node — fail loud with the exact remedy rather than silently producing blanks."""
    # search cwd and the canonical ECHELON estate for a .flux/server.json
    candidates = [Path.cwd() / ".flux" / "server.json",
                  _estate.estate_root_for("command_root") / ".flux" / "server.json"]
    for c in candidates:
        if c.is_file():
            data = json.loads(c.read_text(encoding="utf-8"))
            if data.get("port"):
                return data
    raise VisionError(
        "no live Flux server found (.flux/server.json missing or port:0). "
        "Start it with the VSCode command 'Flux: Start Server' in the estate, then retry.")


class Eye:
    """The scripted mirror eye over one Flux server. Holds ONE session handle for its whole life."""

    def __init__(self, root: str):
        self.root = root.rstrip("/")          # http://127.0.0.1:PORT/api/v1
        self.bus = self.root + "/bus"
        self.session: str | None = None
        self.secret: str | None = None
        self.mirror_url: str | None = None

    # ── transport ──────────────────────────────────────────────────────────
    def _get(self, path: str, **q) -> dict:
        url = self.root + path + ("?" + urllib.parse.urlencode(q) if q else "")
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode())

    def _post(self, obj: dict, **q) -> dict:
        url = self.bus + ("?" + urllib.parse.urlencode(q) if q else "")
        req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    @staticmethod
    def _payload(resp: dict) -> dict:
        return resp.get("envelope", resp).get("payload", resp)

    # ── §5b step 1: CREATE (once) ──────────────────────────────────────────
    def create(self, target: str, mode: str = "verify-fix") -> None:
        if self.session:
            raise VisionError("session already minted — re-minting is the cardinal sin (bug #2). Reuse it.")
        resp = self._post({"kind": "session.create",
                            "payload": {"target": target, "scope": "nav", "mode": mode, "actor": "llm"}})
        p = self._payload(resp)
        self.session, self.secret = p.get("session"), p.get("secret")
        self.mirror_url = p.get("mirror_url")
        if not self.session:
            raise VisionError(f"session.create returned no handle: {json.dumps(resp)[:300]}")

    # ── §5b step 2: ANCHOR (forces off about:blank; skipping = bug #1) ──────
    def anchor(self) -> dict:
        dom = self._get("/bus/dom", session=self.session, settle="dom")
        p = self._payload(dom)
        if not p.get("settled"):
            print("  ! anchor DOM did not settle (continuing, but the surface may be mid-load)", file=sys.stderr)
        html = p.get("html", "")
        if not html:
            raise VisionError("anchor returned empty DOM — the tab never reached the target (bug #1).")
        return p

    # ── §5b step 3: DRIVE (eval; result is TOP-LEVEL on the live build) ─────
    def eval_js(self, js: str):
        resp = self._post({"kind": "hand", "payload": {"op": "eval", "js": js}},
                          session=self.session, secret=self.secret)
        if not resp.get("ok", True):
            raise VisionError(f"eval rejected: {json.dumps(resp)[:300]}")
        return resp.get("result")            # verified: live build returns the value at top level

    # ── §5b step 4: BLINK (settled non-empty PNG is the witness) ───────────
    def blink(self, out_path: Path) -> int:
        bl = self._get("/bus/blink", session=self.session, settle="dom")
        p = self._payload(bl)
        b64 = p.get("png_b64") or p.get("image") or p.get("data")
        if not b64:
            disk = p.get("path")
            if disk and os.path.exists(disk):
                raw = Path(disk).read_bytes()      # fall back to the on-disk blink the server wrote
            else:
                raise VisionError(f"blink produced no image (keys={list(p.keys())}).")
        else:
            raw = base64.b64decode(b64.split(",")[-1])
        if len(raw) < 1000:
            raise VisionError(f"blink PNG is suspiciously small ({len(raw)} bytes) — a blank surface.")
        out_path.write_bytes(raw)
        return len(raw)

    # ── §5b step 6: END ────────────────────────────────────────────────────
    def end(self) -> None:
        if self.session:
            try:
                self._post({"kind": "session.end", "payload": {}},
                          session=self.session, secret=self.secret)
            except Exception:
                pass
            self.session = None


# the JS that clicks a nav item whose visible text contains <label> (ES5 so it runs in any page runtime)
_CLICK_NAV_TMPL = (
    "(function(){{var ns=document.querySelectorAll({nav!r});"
    "for(var i=0;i<ns.length;i++){{if((ns[i].textContent||'').indexOf({label!r})>=0){{ns[i].click();return true;}}}}"
    "return false;}})()"
)
_ACTIVE_PAGE = "(function(){var p=document.querySelector('.page.active, [data-active-page]');return p?(p.id||'active'):'NONE';})()"


def _digest(p: Path) -> str:
    import hashlib
    return hashlib.md5(p.read_bytes()).hexdigest()[:12]


def run(target: str, routes: list[str], out_dir: Path, nav_sel: str, mode: str, hash_routes: bool) -> int:
    """The pipeline. Two routing modes, both asserting every protocol witness (any miss = VisionError, LOUD):
      • hash_routes=True  (SPA, client-side routing): blink re-navigates to the session's target URL, so the
        route MUST live in the URL. We mint one session per `target#route` — blink lands on the right page.
        (mirror.py:2804 `canvas_url = real_target`: blink shoots the target, not the live driven tab.)
      • hash_routes=False (server-routed app): one session, anchor once, eval-click each nav label, blink.
    A STALE-FRAME GUARD then asserts the shots actually differ — the loud catch for "N identical PNGs"."""
    srv = _server_json()
    api = srv["bus"].rsplit("/bus", 1)[0]         # .../api/v1
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    plan = routes or ["__idle__"]

    if hash_routes:
        for route in plan:
            eye = Eye(api)
            url = target if route == "__idle__" else f"{target}#{route}"
            try:
                eye.create(url, mode=mode)
                eye.anchor()                       # bug #1 guard — forces off about:blank onto target#route
                name = "idle" if route == "__idle__" else route
                png = out_dir / f"{name}.png"
                n = eye.blink(png)                 # settled non-empty PNG is the witness
                written.append(png)
                print(f"  blink {name:14s} ({url})  -> {png}  ({n} bytes)")
            finally:
                eye.end()
    else:
        eye = Eye(api)
        try:
            eye.create(target, mode=mode)
            print(f"[vision] session {eye.session} on {target}")
            print(f"[vision] watch live: {eye.mirror_url}")
            eye.anchor()
            for route in plan:
                if route != "__idle__":
                    landed = eye.eval_js(_CLICK_NAV_TMPL.format(nav=nav_sel, label=route))
                    if landed is False:
                        print(f"  ! route {route!r}: no nav item matched (skipping)", file=sys.stderr)
                        continue
                    time.sleep(0.4)
                    print(f"  route {route:14s} -> active page: {eye.eval_js(_ACTIVE_PAGE)}")
                name = "idle" if route == "__idle__" else route
                png = out_dir / f"{name}.png"
                n = eye.blink(png)
                written.append(png)
                print(f"  blink {name:14s} -> {png}  ({n} bytes)")
        finally:
            eye.end()

    # ── STALE-FRAME GUARD: the loud catch this verb exists for ──────────────
    if len(written) > 1:
        digs = {p.name: _digest(p) for p in written}
        if len(set(digs.values())) == 1:
            raise VisionError(
                f"all {len(written)} shots are IDENTICAL ({next(iter(digs.values()))}) — the eye captured "
                f"the same frame for every route. The surface is NOT routing by URL; use --hash for an SPA, "
                f"or the routes never changed the page. Shots are untrustworthy.")
    print(f"[vision] {len(written)} shot(s) -> {out_dir}")
    return 0


def _main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon vision",
        description="Scripted Flux mirror eye — accurate, trap-free screenshots of any web surface. "
                    "Encodes the FLUX_STEERING_PROTOCOL §5b correct sequence and fails LOUD on any "
                    "protocol-witness miss (no session / unanchored / empty blink).")
    ap.add_argument("--target", required=True,
                    help="the surface to see: a file:/// path or an http(s) URL")
    ap.add_argument("--routes", default="",
                    help="comma list of nav LABELS to click+shoot in order (default: just the idle surface)")
    ap.add_argument("--out", default="_vision_shots",
                    help="output dir for the PNGs (created; default _vision_shots)")
    ap.add_argument("--nav", default=".nav-item",
                    help="CSS selector for the nav items to match labels against (default .nav-item)")
    ap.add_argument("--mode", default="verify-fix",
                    help="flux session mode (default verify-fix; moded sessions require --target)")
    ap.add_argument("--hash", action="store_true",
                    help="SPA mode: route via URL fragment (target#route) so blink lands on the right page")
    a = ap.parse_args(argv)
    routes = [r.strip() for r in a.routes.split(",") if r.strip()]
    try:
        return run(a.target, routes, Path(a.out), a.nav, a.mode, a.hash)
    except VisionError as e:
        print(f"[vision] FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(_main())
