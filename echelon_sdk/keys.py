"""Key loading — resolve provider API keys from standard locations.

Search order (first wins):
  1. Process environment variable (DEEPSEEK_API_KEY, etc.)
  2. <ECHELON_HOME>/.env  (defaults to ~/.echelon/.env — the deployed-install store)
  3. Repo root .apikey (dev convenience — won't exist on user machines)
  4. ECHELON_ROOT env var → <root>/.apikey

The .apikey and .env files are KEY=VALUE env-lines, NOT raw keys.

MULTI-TENANT BOUNDARY (owner 2026-08-15): step 2 resolves through ECHELON_HOME on
EVERY call, not once at import. On the single-port MCP gateway each request runs with
the caller's own ECHELON_HOME, so this is what makes a member's paid call resolve the
MEMBER's key instead of the box operator's.

This was a live money leak, found by a skeptic gate and reproduced: `_HOME_ENV` was
computed at import time as `Path.home()/".echelon"/".env"`, so a member subprocess —
even with its provider env vars deliberately stripped and ECHELON_HOME pointed at its
own bank — still read the OWNER's ~/.echelon/.env off disk and spent the owner's key.
Stripping the environment could never have fixed that; the fallback is on disk.
Isolation must be resolved where the file is CHOSEN, not where the env is built.

ISOLATION=1 (ECHELON_KEYS_ISOLATED): drop the dev-convenience fallbacks (3) and (4)
entirely. A shared gateway sets this so a member can never reach a repo/estate .apikey
that happens to sit on the box.
"""
from __future__ import annotations
import os
from pathlib import Path

# Repo root .apikey — dev convenience, resolved relative to this file
_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_APIKEY = _REPO_ROOT / ".apikey"


def _home_env() -> Path:
    """<ECHELON_HOME>/.env, resolved PER CALL (see the multi-tenant note above)."""
    override = os.environ.get("ECHELON_HOME")
    base = Path(override).expanduser() if override else (Path.home() / ".echelon")
    return base / ".env"


def _isolated() -> bool:
    """True when only the caller's own home may supply keys (shared gateways)."""
    return os.environ.get("ECHELON_KEYS_ISOLATED", "").strip().lower() in ("1", "true", "yes")


def _echelon_apikey() -> Path | None:
    """The estate .apikey, if ECHELON_ROOT is set."""
    env = os.environ.get("ECHELON_ROOT")
    if env:
        return Path(env) / ".apikey"
    return None


def _key_files() -> list[Path]:
    """All key files in search order. Existence checked at read time."""
    files: list[Path] = []
    home = _home_env()
    if home.exists():
        files.append(home)
    if _isolated():
        # shared gateway: the caller's own home is the ONLY key source
        return files
    if _REPO_APIKEY.exists():
        files.append(_REPO_APIKEY)
    estate = _echelon_apikey()
    if estate and estate.exists():
        files.append(estate)
    return files


def _read_named_key(var: str, files: list[Path] | None = None) -> str | None:
    """Find `VAR=value` across key files (and the process env). Returns the value
    (everything after the first '='), or None."""
    # Process env wins first (lets a session override without touching files)
    if os.environ.get(var):
        return os.environ[var].strip()
    for p in (files or _key_files()):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip().upper() == var.upper():
                    return v.strip()
        except (FileNotFoundError, PermissionError):
            continue
    return None


# ── Key loaders ───────────────────────────────────────────────────────────────

def load_xai_key(path: Path | None = None) -> str:
    """Return the raw xai-... Bearer token."""
    if path is not None:
        raw = path.read_text(encoding="utf-8").strip()
        if "=" in raw:
            raw = raw.split("=", 1)[1].strip()
    else:
        raw = _read_named_key("Xai_API_KEY") or _read_named_key("XAI_API_KEY") or ""
    if not raw.startswith("xai-"):
        raise ValueError(
            "xAI key not found. Set XAI_API_KEY env var, add to ~/.echelon/.env, "
            "or run: echelon config set-key xai <key>")
    return raw


def load_deepseek_key(path: Path | None = None) -> str:
    """Return the raw sk-... DeepSeek Bearer token."""
    raw = _read_named_key("DEEPSEEK_API_KEY", [path] if path else None) or ""
    if not raw.startswith("sk-"):
        raise ValueError(
            "DeepSeek key not found. Set DEEPSEEK_API_KEY env var, add to ~/.echelon/.env, "
            "or run: echelon config set-key deepseek <key>")
    return raw


def load_gemini_key(path: Path | None = None) -> str:
    """Return the Gemini Vertex Express key (AQ.Ab8... format).

    Vertex Express keys drive Vertex AI with a plain API key — no gcloud, no
    service account. The genai SDK with vertexai=True routes to
    aiplatform.googleapis.com automatically.
    """
    raw = (_read_named_key("GEMINI_API_VERTEX", [path] if path else None)
           or _read_named_key("GEMINI_CHAT_FREE")
           or _read_named_key("GEMINI")
           or _read_named_key("GEMINI_API_KEY") or "")
    if not raw:
        raise ValueError(
            "Gemini key not found. Set GEMINI_API_VERTEX env var, add to ~/.echelon/.env, "
            "or run: echelon config set-key gemini <key>")
    return raw


def load_anthropic_key(path: Path | None = None) -> str:
    """Return the Anthropic API key (sk-ant-...)."""
    raw = _read_named_key("ANTHROPIC_AUTH_TOKEN", [path] if path else None) or ""
    if not raw:
        raise ValueError(
            "Anthropic key not found. Set ANTHROPIC_AUTH_TOKEN env var, add to ~/.echelon/.env, "
            "or run: echelon config set-key anthropic <key>")
    return raw


def load_lmstudio_key(path: Path | None = None) -> str:
    """Return the LM Studio Bearer token for remote OpenAI-compatible servers."""
    raw = _read_named_key("LM_API_TOKEN", [path] if path else None) or ""
    if not raw:
        raise ValueError(
            "LM Studio token not found. Set LM_API_TOKEN env var, add to ~/.echelon/.env, "
            "or run: echelon config set-key lmstudio <key>")
    return raw


# ── Key management ────────────────────────────────────────────────────────────

def keys_summary() -> dict[str, str]:
    """Check all known provider keys. Returns {provider: 'ready' | 'missing'}."""
    providers = {
        "deepseek": ("DEEPSEEK_API_KEY", load_deepseek_key),
        "gemini": ("GEMINI_API_VERTEX", load_gemini_key),
        "anthropic": ("ANTHROPIC_AUTH_TOKEN", load_anthropic_key),
        "xai": ("XAI_API_KEY", load_xai_key),
        "lmstudio": ("LM_API_TOKEN", load_lmstudio_key),
    }
    result = {}
    for name, (env_var, loader) in providers.items():
        try:
            loader()
            result[name] = "ready"
        except ValueError:
            result[name] = "missing"
    return result


def write_key(provider: str, key: str) -> Path:
    """Write a provider API key to ~/.echelon/.env. Creates the file if needed.

    Args:
        provider: Provider name (deepseek, gemini, anthropic, xai, lmstudio)
        key: The raw API key value

    Returns the path written to.
    """
    env_vars = {
        "deepseek": "DEEPSEEK_API_KEY",
        "gemini": "GEMINI_API_VERTEX",
        "anthropic": "ANTHROPIC_AUTH_TOKEN",
        "xai": "XAI_API_KEY",
        "lmstudio": "LM_API_TOKEN",
    }
    var = env_vars.get(provider)
    if not var:
        raise ValueError(f"Unknown provider: {provider}. Known: {', '.join(env_vars)}")

    # Writes to the CALLER'S home (ECHELON_HOME-aware, same resolution as reads) —
    # so `echelon config set-key` run by a member lands in THEIR bank, not the box's.
    home_env = _home_env()
    home_env.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if home_env.exists():
        lines = home_env.read_text(encoding="utf-8").splitlines()
    # Update or append
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{var}="):
            lines[i] = f"{var}={key}"
            found = True
            break
    if not found:
        lines.append(f"{var}={key}")
    # Write atomically
    tmp = home_env.with_suffix(".tmp")
    tmp.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    tmp.replace(home_env)
    try:
        home_env.chmod(0o600)   # a key file is not world-readable
    except OSError:
        pass
    return home_env
