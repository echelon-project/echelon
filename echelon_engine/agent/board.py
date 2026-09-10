from __future__ import annotations

import threading
import time as _time
from typing import Any, Callable


def _default_provider_model() -> tuple[Any, str]:
    try:
        from echelon_sdk.config import get as _cfg_get
        brain = _cfg_get("brain") or "grok"
    except Exception:
        brain = "grok"
    return _resolve_provider_model(brain)


def _resolve_provider_model(name: str) -> tuple[Any, str]:
    """Resolve a provider name string to (provider_object, default_model)."""
    name = (name or "grok").lower()
    if name in ("deepseek", "deep"):
        from echelon_engine.atoms.providers.deepseek import DeepSeekProvider
        return DeepSeekProvider(), "deepseek-chat"
    if name in ("gemini", "gem"):
        from echelon_engine.atoms.providers.gemini import GeminiProvider
        return GeminiProvider(), "gemini-2.5-flash"
    if name in ("anthropic", "claude"):
        from echelon_engine.atoms.providers.anthropic_upstream import AnthropicUpstreamProvider
        import os
        from echelon_sdk.keys import load_anthropic_key
        key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or load_anthropic_key()
        return AnthropicUpstreamProvider(upstream_url="https://api.anthropic.com", api_key=key), "claude-sonnet-4-6"
    # default: grok
    from echelon_engine.atoms.providers.grok import GrokProvider
    return GrokProvider(), "grok-4.3"


def _make_partner_runner(
    provider,
    model: str,
    folder: str,
    *,
    role: str = "dev",
    guidance: str | None = None,
    files: list[str] | None = None,
    budget=None,
    max_steps: int = 60,
    on_event: Callable[[str, dict], None] | None = None,
) -> Callable[[dict, dict], dict]:
    from .workflow import make_agent_runner

    return make_agent_runner(
        provider,
        model,
        folder,
        budget=budget,
        max_steps=max_steps,
        guidance=guidance,
        files=files,
        on_event=on_event,
    )


class Ledger:
    def __init__(self, *, scope: str | None = None, books_home: str | None = None) -> None:
        self._lock = threading.Lock()
        self._claims: dict[str, str] = {}
        self._log: list[dict[str, Any]] = []
        self._posted: dict[str, dict] = {}
        self._landed: set[str] = set()
        self._books: dict[str, Any] | None = None
        self._scope = scope
        if scope is not None:
            self.bind_books(scope, home=books_home)

    def bind_books(self, scope: str, *, roles: list[str] | None = None, home: str | None = None) -> None:
        from .rolebook import CARD_ORDER, RoleBook

        self._scope = scope
        self._books = {r: RoleBook(scope, r, home=home) for r in (roles or CARD_ORDER)}

    def claim(self, goal_id: str, partner: str) -> bool:
        with self._lock:
            if goal_id in self._claims:
                return self._claims[goal_id] == partner
            self._claims[goal_id] = partner
            self._log.append({"op": "claim", "goal": goal_id, "partner": partner, "t": _time.time()})
            return True

    def land(self, goal_id: str, partner: str, result: Any) -> dict[str, Any]:
        with self._lock:
            entry = {
                "op": "land",
                "goal": goal_id,
                "partner": partner,
                "result": result,
                "seq": len(self._log),
                "t": _time.time(),
            }
            self._log.append(entry)
            self._landed.add(goal_id)
            return dict(entry)

    def act(
        self,
        goal_id: str,
        partner: str,
        role: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        artifact_ref: str | None = None,
    ) -> None:
        with self._lock:
            self._log.append(
                {
                    "op": "act",
                    "goal": goal_id,
                    "partner": partner,
                    "role": role,
                    "kind": kind,
                    "artifact_ref": artifact_ref,
                    "seq": len(self._log),
                    "t": _time.time(),
                }
            )
        book = self._books.get(role) if self._books is not None else None
        if book is not None:
            try:
                book.record_action(goal_id, partner, kind, payload, artifact_ref=artifact_ref)
            except Exception:
                pass

    def post(
        self,
        goal_id: str,
        by: str,
        task: str,
        *,
        role: str = "dev",
        rules: str | None = None,
        parent: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if goal_id in self._posted:
                return dict(self._posted[goal_id])
            rec = {
                "id": goal_id,
                "task": task,
                "role": role,
                "rules": rules,
                "by": by,
                "parent": parent,
                "seq": len(self._log),
                "t": _time.time(),
            }
            self._posted[goal_id] = rec
            self._log.append(
                {
                    "op": "post",
                    "goal": goal_id,
                    "partner": by,
                    "task": task,
                    "parent": parent,
                    "seq": len(self._log),
                    "t": _time.time(),
                }
            )
            return dict(rec)

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for gid, r in self._posted.items() if gid not in self._landed]

    def read(self, goal_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            lands = [dict(e) for e in self._log if e["op"] == "land"]
        return [e for e in lands if goal_id is None or e["goal"] == goal_id]

    @property
    def landings(self) -> list[dict[str, Any]]:
        return self.read()


def run_board(
    goals: list,
    *,
    scope: str = "echelon",
    folder: str | None = None,
    n_partners: int = 2,
    provider=None,
    model: str | None = None,
    db_path: str | None = None,
    cartridges: list[str] | None = None,
    tier_scope: str | None = None,
    simulate_double_land: bool = False,
    simulate_race: bool = False,
    default_provider_model: Callable[[], tuple[Any, str]] | None = None,
    make_partner_runner: Callable[..., Callable[[dict, dict], dict]] | None = None,
) -> dict[str, Any]:
    items = [{"id": g} if isinstance(g, str) else dict(g) for g in goals]
    ledger = Ledger(scope=scope)

    if simulate_race:
        gid = items[0]["id"] if items else "g0"
        first = ledger.claim(gid, "partner-A")
        second = ledger.claim(gid, "partner-B")
        claims_unique = first is True and second is False
        return {
            "ledger": ledger.read(),
            "landed": len(ledger.landings),
            "claims_unique": claims_unique,
            "n_partners": n_partners,
        }

    if simulate_double_land:
        gid = items[0]["id"] if items else "g0"
        ledger.claim(gid, "partner-A")
        ledger.land(gid, "partner-A", "v1")
        ledger.land(gid, "partner-A", "v2")
        lands = ledger.read(gid)
        append_only_ok = (
            len(lands) == 2
            and lands[0]["result"] == "v1"
            and lands[1]["result"] == "v2"
            and lands[1]["seq"] > lands[0]["seq"]
        )
        return {
            "ledger": ledger.read(),
            "landed": len(ledger.landings),
            "append_only_ok": append_only_ok,
            "n_partners": n_partners,
        }

    if not folder:
        for i, it in enumerate(items):
            partner = f"partner-{i % max(1, n_partners)}"
            if ledger.claim(it["id"], partner):
                ledger.land(it["id"], partner, {"status": "dry-run", "task": it.get("task", it["id"])})
        return {
            "ledger": ledger.read(),
            "landed": len(ledger.landings),
            "claims_unique": True,
            "append_only_ok": True,
            "n_partners": n_partners,
        }

    from .fork_field import make_bank_pager, run_fork_field
    from echelon_engine.atoms.store import SeedStore
    from .rolebook import compose_card_on_done, role_of

    default_provider_model = default_provider_model or _default_provider_model
    make_partner_runner = make_partner_runner or _make_partner_runner

    if provider is None:
        provider, _dm = default_provider_model()
        model = model or _dm
    elif isinstance(provider, str):
        provider, _dm = _resolve_provider_model(provider)
        model = model or _dm
    model = model or "grok-4.3"
    _plug = list(cartridges or ["craft"])

    store = SeedStore(db_path) if db_path else SeedStore()
    pager = make_bank_pager(store, scope)
    base_guidance = (
        f"ECHELON-equipped partner in-scope (`{scope}`), craft cartridge plugged in. You share a "
        f"BOARD with the other partners: CLAIM your record before acting, LAND your result when "
        f"done, and READ the board to see what others landed before you build on it. Act directly."
    )
    run_step = make_partner_runner(provider, model, folder, guidance=base_guidance)

    def _coordinated(step: dict, deps: dict, **kw) -> dict:
        gid = step["id"]
        partner = step.get("agent", "dev")
        role = role_of(step.get("agent", "dev"))
        if not ledger.claim(gid, partner):
            return {"status": "skipped", "answer": "record already claimed by another partner", "steps": 0}
        ledger.act(gid, partner, role, "step_start", {"task": (step.get("task") or "")[:120]})
        res = run_step(step, deps, **kw)
        ledger.act(
            gid,
            partner,
            role,
            "step_done",
            {
                "status": res.get("status"),
                "steps": res.get("steps"),
                "answer": (res.get("answer") or "")[:300],
            },
            artifact_ref=res.get("folder") or folder,
        )
        ledger.land(gid, partner, {"status": res.get("status"), "answer": (res.get("answer") or "")[:600]})
        return res

    steps = [
        {
            "id": it["id"],
            "agent": it.get("role", "dev"),
            "task": it.get("task", it["id"]) + (f"\nRULES: {it['rules']}" if it.get("rules") else ""),
            "deps": [],
        }
        for it in items
    ]
    trace = run_fork_field({"steps": steps}, run_step=_coordinated, warmth_of=pager)

    lands = ledger.read()
    outcome_ok = bool(lands) and all(
        (e.get("result") or {}).get("status") in ("completed", "done", "success") for e in lands
    )
    earned = compose_card_on_done(
        scope,
        "; ".join(it.get("task", it["id"]) for it in items),
        outcome_ok=outcome_ok,
        books=ledger._books,
        tier_scope=tier_scope,
        db_path=db_path,
    )
    return {
        "scope": scope,
        "n": len(items),
        "ledger": lands,
        "landed": len(ledger.landings),
        "trace": trace,
        "_ledger": ledger,
        "outcome_ok": outcome_ok,
        "earned": earned,
    }
