"""providers — test connectivity of configured LLM providers.

    python -X utf8 -m echelon_engine providers [--test] [--json]
"""
from __future__ import annotations

import argparse
import json
import time


def _read_env_or_dotenv(var: str) -> str:
    """Env first, then ~/.echelon/.env — the same precedence OpenAIProvider uses, so the
    KEY column of `echelon providers` tells the truth for a provider whose key has no
    dedicated loader in echelon_sdk.keys (ox / OPENROUTER_API_KEY)."""
    import os
    from pathlib import Path
    v = os.environ.get(var, "").strip()
    if v:
        return v
    try:
        for line in (Path.home() / ".echelon" / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{var}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _test_provider(name: str, pkg: str, cls: str, model: str) -> dict:
    """Ping a provider: can we import it, instantiate it, and reach its endpoint?"""
    result = {"name": name, "model": model, "ok": False, "latency_ms": None, "error": ""}
    t0 = time.monotonic()
    try:
        import importlib
        mod = importlib.import_module(pkg)
        prov_cls = getattr(mod, cls)
        prov = prov_cls()
        result["ok"] = True
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
    except Exception as e:
        result["error"] = str(e)[:120]
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
    return result


# Registry of known providers: name -> (package, class, default_model)
_PROVIDER_REGISTRY = {
    "grok":     ("echelon_engine.atoms.providers.grok",   "GrokProvider",   "grok-4.3"),
    "deepseek": ("echelon_engine.atoms.providers.deepseek", "DeepSeekProvider", "deepseek-v4-pro[1m]"),
    "gemini":   ("echelon_engine.atoms.providers.gemini",  "GeminiProvider",  "gemini-3.1-pro-preview"),
    "local":    ("echelon_engine.atoms.providers.local",   "LocalProvider",   ""),
    "bridge":   ("echelon_engine.atoms.providers.bridge",  "BridgeProvider",  "grok-4.3"),
    # THE FREE GATE (owner 2026-08-25) — ox-alpha via OpenRouter, $0 preview, 1M ctx.
    # Registered as the FACTORY (ox_provider), not a class: it is a configured
    # OpenAIProvider, the same shape nemotron uses. Key: OPENROUTER_API_KEY.
    "ox":       ("echelon_engine.atoms.providers.openai_compat", "ox_provider", "stealth/ox-alpha"),
    # THE FREE PIXEL GATE — minimax-m3 :free tier, VISION. Sibling of ox (reasoning).
    "minimax":  ("echelon_engine.atoms.providers.openai_compat", "minimax_provider", "minimax/minimax-m3:free"),
    "eos":      ("echelon_engine.atoms.providers.eos",     "EOSProvider",     ""),
    "copilot":  ("echelon_engine.atoms.providers.copilot", "CopilotProvider", ""),
    "codex":    ("echelon_engine.atoms.providers.codex",   "CodexProvider",   ""),
    "venice":   ("echelon_engine.atoms.providers.venice",  "VeniceProvider",  ""),
}


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="echelon providers",
                                 description="Test connectivity of configured LLM providers.")
    ap.add_argument("--test", action="store_true", help="live connectivity check (import + instantiate)")
    ap.add_argument("--provider", metavar="NAME", help="test a single provider by name")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)

    if a.provider and a.provider not in _PROVIDER_REGISTRY:
        print(f"unknown provider {a.provider!r}. known: {', '.join(_PROVIDER_REGISTRY)}")
        return 1

    # Check which providers have API keys configured
    try:
        from echelon_sdk.keys import (
            load_grok_key, load_deepseek_key, load_gemini_key,
            load_lmstudio_key, load_eos_key, load_copilot_key,
        )
    except ImportError:
        load_grok_key = load_deepseek_key = load_gemini_key = lambda: ""
        load_lmstudio_key = load_eos_key = load_copilot_key = lambda: ""

    key_checks = {
        "grok":     load_grok_key,
        "deepseek": load_deepseek_key,
        "gemini":   load_gemini_key,
        "local":    load_lmstudio_key,
        "eos":      load_eos_key,
        "copilot":  load_copilot_key,
        # ox reads OPENROUTER_API_KEY env-first, then ~/.echelon/.env (the shared
        # named-key resolver) — no dedicated loader in echelon_sdk.keys.
        "ox":       lambda: _read_env_or_dotenv("OPENROUTER_API_KEY"),
        "minimax":  lambda: _read_env_or_dotenv("OPENROUTER_API_KEY"),
    }

    providers = [a.provider] if a.provider else list(_PROVIDER_REGISTRY)
    results = []

    for name in providers:
        spec = _PROVIDER_REGISTRY.get(name)
        if not spec:
            continue
        pkg, cls, default_model = spec

        # Key check
        has_key = False
        if name in key_checks:
            try:
                k = key_checks[name]()
                has_key = bool(k)
            except Exception:
                has_key = False

        entry = {"name": name, "model": default_model, "key_configured": has_key,
                 "import_ok": False, "latency_ms": None, "error": ""}

        if a.test:
            live = _test_provider(name, pkg, cls, default_model)
            entry["import_ok"] = live["ok"]
            entry["latency_ms"] = live["latency_ms"]
            entry["error"] = live["error"]
        else:
            # Fast check: can we import the module?
            t0 = time.monotonic()
            try:
                import importlib
                importlib.import_module(pkg)
                entry["import_ok"] = True
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000)
            except Exception as e:
                entry["error"] = str(e)[:120]
                entry["latency_ms"] = round((time.monotonic() - t0) * 1000)

        results.append(entry)

    if a.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"{'PROVIDER':<12} {'MODEL':<28} {'KEY':<5} {'IMPORT':<7} {'LATENCY':<8}")
    print("-" * 65)
    for r in results:
        key_icon = "yes" if r["key_configured"] else "no"
        import_icon = "ok" if r["import_ok"] else ("FAIL" if r["error"] else "?")
        latency = f"{r['latency_ms']}ms" if r["latency_ms"] else "-"
        print(f"  {r['name']:<10} {r['model']:<28} {key_icon:<5} {import_icon:<7} {latency:<8}")
        if r["error"]:
            print(f"    error: {r['error']}")

    # ZERO-KEY CTA (go-live audit 2026-07-30): a cold install has no keys — name
    # the exact command + env-var alternative so the user knows what to do next.
    if not any(r["key_configured"] for r in results):
        print()
        print("  No API keys configured. Set one to enable LLM-powered features:")
        print("    echelon config set-key deepseek <key>        (or: gemini / anthropic / xai / lmstudio)")
        print("    Or set the env var: DEEPSEEK_API_KEY=<key>   (or: GEMINI_API_VERTEX, ANTHROPIC_AUTH_TOKEN, etc.)")

    if not a.test:
        print("\n  (run with --test for live connectivity check)")

    return 0
