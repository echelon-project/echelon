"""config_report.py — the comprehensive ECHELON report (status-card class, but full).

Owner 2026-06-07: "I want the full report, like the status card, but more comprehensive." Surfaces
the WHOLE live surface in one call: the config registry, the substrate census (uame.stats), the
routing ladder, swarm capacity, and paths. A read-only instrument — it invents nothing, it reads the
live organs (config.all_config, store.stats, routing.pick). Run:
  python -X utf8 -m echelon_engine.atoms.config_report          # text
  python -X utf8 -m echelon_engine.atoms.config_report --json   # machine-readable
"""
from __future__ import annotations
import json
import sys


def gather() -> dict:
    """Read every live surface into one dict. Each block degrades to an error string, never crashes."""
    out: dict = {}
    try:
        from echelon_sdk import config
        out["config"] = config.all_config()
    except Exception as e:  # noqa: BLE001
        out["config"] = {"error": repr(e)}
    try:
        from echelon_engine.atoms.store import SeedStore
        s = SeedStore()
        out["substrate"] = s.stats()
        out["substrate"]["echelon_scope"] = s.count(scope="echelon")
    except Exception as e:  # noqa: BLE001
        out["substrate"] = {"error": repr(e)}
    try:
        from echelon_engine.atoms import routing
        out["routing"] = {r: {"model": routing.pick(r), "bridge": routing.via_bridge(r)}
                          for r in ("driver", "judge", "audit", "reason", "vision")}
    except Exception as e:  # noqa: BLE001
        out["routing"] = {"error": repr(e)}
    try:
        from echelon_sdk import paths
        out["paths"] = {"home": str(paths.HOME), "core_db": str(paths.CORE_DB),
                        "runs": str(paths.RUNS), "bridges": str(paths.BRIDGES)}
    except Exception as e:  # noqa: BLE001
        out["paths"] = {"error": repr(e)}
    return out


def render(data: dict) -> str:
    L = ["=" * 64, "  ECHELON — COMPREHENSIVE REPORT", "=" * 64]
    c = data.get("config", {})
    if "error" not in c:
        L.append("")
        L.append("[ CONFIG REGISTRY ]  (~/.echelon/config.json overrides; fallbacks in config.py)")
        L.append(f"  brain={c.get('brain')}  mode={c.get('mode')}  soul={c.get('soul')}  "
                 f"max_steps={c.get('max_steps')}  ttl={c.get('ttl')}  budget=${c.get('budget_usd')}")
        L.append(f"  call_timeout={c.get('call_timeout')}s  tier_order={c.get('tier_order')}")
        # The LM floor host — the cartridge-portability seam (env LM_ENDPOINT > floor.host > fallback).
        # Surface the EFFECTIVE host (post-env-override), not just the JSON value, so the report tells
        # the truth about where the providers/embedder/atomizer are actually pointed right now.
        try:
            from echelon_sdk import config as _cfg
            L.append(f"  floor.host={_cfg.floor_host()}  (LM_ENDPOINT env > floor.host config > fallback)")
        except Exception:  # noqa: BLE001
            L.append(f"  floor.host={c.get('floor', {}).get('host')}")
        ff = c.get("fork_field", {})
        L.append(f"  fork_field: decay={ff.get('decay')} cold={ff.get('cold')} "
                 f"max_parallel={ff.get('max_parallel')} max_ticks={ff.get('max_ticks')}")
        sw = c.get("swarm", {})
        L.append(f"  swarm: max_agents={sw.get('max_agents')} attach={sw.get('attach_max_agents')} "
                 f"workflow_parallel={sw.get('workflow_max_parallel')} sub_ttl={sw.get('sub_ttl')}")
        sc = c.get("score", {})
        w = c.get("warmth", {})
        fr = c.get("file_read", {})
        L.append(f"  score: B={sc.get('benchmark')} lambda={sc.get('lambda')} "
                 f"promote>={sc.get('promote_threshold')} & {sc.get('promote_min_recalls')} recalls")
        L.append(f"  warmth: warm>={w.get('warm')} lukewarm>={w.get('lukewarm')} "
                 f"edge_decay={w.get('edge_decay')} max_hops={w.get('max_hops')}")
        L.append(f"  file_read: mode={fr.get('mode')} window={fr.get('window_bytes')}B "
                 f"peek={fr.get('peek_bytes')}B")
    sub = data.get("substrate", {})
    if "error" not in sub:
        L.append("")
        L.append("[ SUBSTRATE CENSUS ]  (the soul, via uame.stats)")
        L.append(f"  soul_total={sub.get('soul_total')}  bank_total={sub.get('bank_total')}  "
                 f"links={sub.get('links')}  echelon_scope={sub.get('echelon_scope')}")
        doms = sub.get("domains", [])
        L.append(f"  domains ({len(doms)}): {', '.join(doms[:12])}" + (" ..." if len(doms) > 12 else ""))
    rt = data.get("routing", {})
    if "error" not in rt:
        L.append("")
        L.append("[ ROUTING LADDER ]")
        for role, v in rt.items():
            L.append(f"  {role:8} -> {v['model']}" + ("  (bridge)" if v.get("bridge") else ""))
    pa = data.get("paths", {})
    if "error" not in pa:
        L.append("")
        L.append("[ PATHS ]")
        L.append(f"  core.db = {pa.get('core_db')}")
    # TIER ACCOUNTING — the architecture (always knowable) + a budget's live per-tier spend (if given).
    L.append("")
    L.append("[ TIER MODEL ]  (who does what + the provider each reaches, per the routing ladder)")
    rt = data.get("routing", {})
    def _m(role):
        v = rt.get(role, {})
        return v.get("model", "?") + ("  (bridge)" if v.get("bridge") else "")
    os_model = data.get("os_model", "claude-opus-4.8")
    L.append(f"  OS  watches/divides/gates/composes -> {os_model}   [THE orchestrator — a real premium")
    L.append(f"      model call (this session); the COSTLIEST tier (full context, every turn). Its spend")
    L.append(f"      runs in the harness, NOT the agent Budget — so it's named-but-external, never faked $0.]")
    L.append(f"  T1  frames+compiles the plan      -> {_m('audit')}   [the strongest brain; framing is intelligence-class]")
    L.append(f"  T2  reasons cheaply per step      -> {_m('judge')} / driver")
    L.append(f"  T3  executes; mechanical=$0 code  -> code-floor (no model) | {_m('reason')} fallback")
    bt = data.get("by_tier")
    os_model = data.get("os_model", "claude-opus-4.8")
    if bt:
        L.append("")
        L.append("[ TIER SPEND — this run's ledger ]")
        # OS first, and ALWAYS present: it's the orchestrator, the costliest tier, external to this
        # Budget (it ran the harness session). Named-but-external — shown, never dropped, never $0.
        L.append(f"  {'OS':9} -> {os_model}  (the session; spend external to the agent Budget — not metered here)")
        for tier, d in bt.items():
            L.append(f"  {tier:9} calls={d['calls']:3} in={d['tokens_in']:>7} out={d['tokens_out']:>6} "
                     f"${d['usd']:.4f}  {d['models']}")
    else:
        L.append("")
        L.append("[ TIER SPEND ]")
        L.append(f"  {'OS':9} -> {os_model}  (the orchestrator session — the dominant, real spend;")
        L.append("              external to the agent Budget, so named-but-not-metered here, never faked $0)")
        L.append("  T1/T2/T3   per-tier $ is recorded only when a run carries a Budget (--budget, tagged")
        L.append("              by tier via Budget.by_tier). This session's swarm dispatches ran the T3")
        L.append("              CODE FLOOR only: 0 model calls, $0 (mechanical writes as code).")
    L.append("=" * 64)
    return "\n".join(L)


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    # Subcommand dispatch
    if argv and argv[0] == "set":
        return _cmd_set(argv[1:])
    if argv and argv[0] == "get":
        return _cmd_get(argv[1:])
    if argv and argv[0] == "set-key":
        return _cmd_set_key(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        _print_config_help()
        return 0

    # Default: full report
    data = gather()
    if "--json" in argv:
        print(json.dumps(data, indent=2))
    else:
        print(render(data))
    return 0


def _print_config_help():
    print("echelon config — read and write ECHELON configuration (~/.echelon/config.json)")
    print()
    print("  echelon config                  full report (read-only)")
    print("  echelon config get [key]        read a config value (or all)")
    print("  echelon config set <key> <val>  write a config value")
    print("  echelon config set-key <p> <k>  store a provider API key in ~/.echelon/.env")
    print()
    print("  Providers for set-key: deepseek, gemini, anthropic, xai, lmstudio")
    print("  Keys are stored in ~/.echelon/.env, never in the config file.")


def _cmd_set(argv) -> int:
    if not argv or len(argv) < 2:
        print("Usage: echelon config set <key> <value>", file=sys.stderr)
        print("  Writes to ~/.echelon/config.json", file=sys.stderr)
        return 1
    key, value = argv[0], " ".join(argv[1:])
    try:
        _write_config(key, value)
        print(f"config: {key} = {value}")
        return 0
    except Exception as e:
        print(f"config set: error: {e}", file=sys.stderr)
        return 1


def _cmd_get(argv) -> int:
    key = argv[0] if argv else None
    try:
        data = _load_config()
        if key:
            val = data.get(key, _FALLBACK_DOTTED(key))
            if val is None:
                print(f"(not set: {key})")
            else:
                print(f"{key} = {val}")
        else:
            for k, v in sorted(data.items()):
                print(f"{k} = {v}")
        return 0
    except Exception as e:
        print(f"config get: error: {e}", file=sys.stderr)
        return 1


def _cmd_set_key(argv) -> int:
    if not argv or len(argv) < 2:
        print("Usage: echelon config set-key <provider> <api-key>", file=sys.stderr)
        print("  Providers: deepseek, gemini, anthropic, xai, lmstudio", file=sys.stderr)
        print("  Stores the key in ~/.echelon/.env", file=sys.stderr)
        return 1
    provider, key = argv[0], " ".join(argv[1:])
    try:
        from echelon_sdk.keys import write_key
        path = write_key(provider, key)
        print(f"config: {provider} key written to {path}")
        return 0
    except Exception as e:
        print(f"config set-key: error: {e}", file=sys.stderr)
        return 1


# ── Config read/write helpers ─────────────────────────────────────────────────

def _load_config() -> dict:
    """Load ~/.echelon/config.json, returning {} if missing."""
    from pathlib import Path
    cfg_path = Path.home() / ".echelon" / "config.json"
    if not cfg_path.exists():
        return {}
    import json
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(key: str, value: str) -> None:
    """Write a single key to ~/.echelon/config.json. Merges with existing."""
    from pathlib import Path
    import json
    cfg_path = Path.home() / ".echelon" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_config()
    # Try to preserve the type: int, float, bool, or string
    if value.lower() in ("true", "false"):
        data[key] = value.lower() == "true"
    else:
        try:
            data[key] = int(value)
        except ValueError:
            try:
                data[key] = float(value)
            except ValueError:
                data[key] = value
    tmp = cfg_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(cfg_path)


def _FALLBACK_DOTTED(key: str):
    """Reach into config.FALLBACKS for a dotted key."""
    try:
        from echelon_sdk.config import FALLBACKS
        parts = key.split(".")
        val = FALLBACKS
        for p in parts:
            val = val[p]
        return val
    except (KeyError, TypeError, ImportError):
        return None


if __name__ == "__main__":
    sys.exit(main())
