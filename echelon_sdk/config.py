"""config.py — the registry-driven config spine (owner 2026-06-07: "config should be in db/json").

Mirrors the routing.json pattern: defaults live in JSON at ~/.echelon/config.json, loaded once at
startup, and the code ALWAYS carries hardcoded fallbacks so a missing/corrupt file degrades safely
(never a crash). One function — get(key, default) — is the single read everything uses, so a default
is data, not a constant scattered across 8 files. Dotted keys address nested groups
(e.g. get("fork_field.max_parallel")). Refresh by editing the JSON; no code change to re-tune.

THE LAW: a default has exactly ONE home (the FALLBACKS table here, overridable by the JSON). A module
that wants a tunable reads config.get("group.key") instead of hardcoding it. See routing.py (the
proven precedent), harvest-raw-access-into-endpoints (config is the same move: surface, then tool it).
"""
from __future__ import annotations
import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".echelon" / "config.json"

# The single source of every default (the registry). The JSON OVERRIDES these; absent -> these stand.
FALLBACKS: dict = {
    "brain": "deepseek",
    "max_steps": 500, "max_drift": 4, "progress_interval": 10,
    "mode": "auto", "soul": "echelon", "ttl": 3600.0,
    "budget_usd": 5.0, "partner_timeout": 900, "split_threshold": 10000,
    "call_timeout": 120.0,
    "fork_field": {"decay": 0.55, "propagate": 0.40, "cold": 0.10, "rewarm_cap": 8,
                   "max_parallel": 18, "max_ticks": 5000},
    "swarm": {"max_agents": 100, "attach_max_agents": 50, "sub_steps": 40, "sub_ttl": 900.0,
              "workflow_max_parallel": 18},
    "score": {"benchmark": 100.0, "lambda": 0.02, "promote_threshold": 125.0, "promote_min_recalls": 3},
    "warmth": {"warm": 0.45, "lukewarm": 0.18, "edge_decay": 0.7, "max_hops": 3, "judge_floor": 0.05},
    "file_read": {"mode": "byte", "small_bytes": 65536, "window_bytes": 16384, "peek_bytes": 2048},
    "tier_order": ["deepseek", "grok", "copilot", "local"],
    "cors": {"origins": ["*"]},  # wildcard for dev; override via ~/.echelon/config.json for prod
    # The local LM Studio floor — ONE home for the host the providers/embedder/atomizer reach.
    # A LAN address is un-portable (a cartridge cannot ship one), so the default is LM Studio's
    # own loopback server. Point it elsewhere in ~/.echelon/config.json, or set LM_ENDPOINT
    # for an ad-hoc override.
    "floor": {"host": "http://127.0.0.1:1234"},
    # LLM_PROVIDER — the config-level provider contract. Name -> (provider_mode, upstream_url, key_loader).
    # proxy.py reads this at startup when --provider is None; the launchers set LLM_PROVIDER env.
    # The key loaders are strings (the function name in echelon_sdk.keys) so config.py stays import-light
    # (no direct key imports — the resolver calls them lazily).
    "llm_provider": {
        "map": {
            "anthropic": {"mode": "upstream", "url": "https://api.anthropic.com", "key_loader": "load_anthropic_key"},
            "deepseek":  {"mode": "upstream", "url": "https://api.deepseek.com/anthropic", "key_loader": "load_deepseek_key"},
            "gemini":    {"mode": "gemini",    "url": None,                                    "key_loader": "load_gemini_key"},
        },
        "default": "anthropic"
    },
}

_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    data = dict(FALLBACKS)
    try:
        if _CONFIG_PATH.exists():
            user = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            data = _deep_merge(data, user)
    except (OSError, json.JSONDecodeError):
        pass  # degrade to fallbacks — config must never crash startup
    _cache = data
    return data


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get(key: str, default=None):
    """Read a config value by dotted key (e.g. 'fork_field.max_parallel'). Falls back to the
    registry default, then the passed default. The single read the whole codebase uses."""
    node = _load()
    for part in key.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


def floor_host() -> str:
    """The local LM Studio floor host (scheme://host:port, no path). Resolution order:
    LM_ENDPOINT env (ad-hoc override) -> config 'floor.host' -> registry fallback. The env
    seam matches local.py's existing precedent so a single export retargets the whole floor."""
    import os
    env = os.environ.get("LM_ENDPOINT")
    if env:
        # LM_ENDPOINT historically carried a full .../v1/chat/completions URL; strip to host.
        return env.split("/v1/")[0].rstrip("/")
    return str(get("floor.host", "http://127.0.0.1:1234")).rstrip("/")


def floor_endpoint(path: str = "/v1/chat/completions") -> str:
    """The floor host joined to an OpenAI-shaped path (chat by default; pass '/v1/embeddings'
    for the embedder). One call replaces every hardcoded floor-host literal."""
    return floor_host() + path


def all_config() -> dict:
    """The whole merged config (for the report / status surface)."""
    return dict(_load())


def reload() -> dict:
    """Drop the cache and re-read (after the JSON changes)."""
    global _cache
    _cache = None
    return _load()


def resolve_llm_provider(name: str | None = None) -> dict:
    """Resolve LLM_PROVIDER name -> {mode, upstream_url, api_key}. Precedence:
    explicit name arg > LLM_PROVIDER env > config llm_provider.default > 'anthropic'.
    Returns a dict ready to feed proxy.py startup: {mode, upstream, api_key}.
    Raises ValueError if the named provider isn't in the map."""
    import os as _os
    resolved = (
        name
        or _os.environ.get("LLM_PROVIDER")
        or get("llm_provider.default", "anthropic")
    )
    pmap = get("llm_provider.map", {})
    entry = pmap.get(resolved)
    if entry is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{resolved}' — known: {list(pmap.keys())}. "
            f"Set llm_provider.map in ~/.echelon/config.json or pass --provider.")
    mode = entry["mode"]
    url = entry.get("url") or ""
    key_loader_name = entry.get("key_loader", "")
    api_key = ""
    if key_loader_name:
        try:
            from echelon_sdk import keys as _keys
            loader = getattr(_keys, key_loader_name, None)
            if loader:
                api_key = loader()
        except Exception:
            pass  # key loading is best-effort at resolve time; proxy reports warning
    return {"mode": mode, "upstream": url, "api_key": api_key}


def llm_provider_names() -> list:
    """List known LLM_PROVIDER names (for help text / validation)."""
    pmap = get("llm_provider.map", {})
    return list(pmap.keys())


def write_default_json(path: Path | None = None) -> Path:
    """Materialize the current FALLBACKS to the JSON (so the file exists to be edited). Idempotent-ish:
    only writes if absent (never clobbers a user-tuned file)."""
    p = path or _CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(json.dumps(FALLBACKS, indent=2), encoding="utf-8")
    return p
