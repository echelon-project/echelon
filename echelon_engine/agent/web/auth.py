"""auth.py — API authentication for the ECHELON live-bridge web server.

Adds a FastAPI dependency (Bearer token) that gates POST/mutation endpoints.
Disabled by default: if ECHELON_API_KEY is unset (env, .apikey, or config),
all requests pass through — backward-compatible with existing consoles.
Set the key and every mutation endpoint requires `Authorization: Bearer <key>`.

Sources (highest priority first):
  1. ECHELON_API_KEY environment variable
  2. ~/.echelon/config.json  key `auth.api_key`
  3. The agent's .apikey file (KEY=VALUE line)
  4. If NONE of these are set → auth is OFF (the require() dependency is a no-op)

Usage in endpoint:
    @app.post("/api/stop")
    async def stop(auth: str = Depends(require_auth)):
        ...
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from echelon_sdk import config
from echelon_sdk.keys import _read_named_key

__all__ = ["require_auth", "get_api_key", "is_enabled"]

_scheme = HTTPBearer(auto_error=False)


def get_api_key() -> str | None:
    """Return the configured API key, or None if auth is not set up.
    Checks: env → config.json → .apikey → None."""
    import os
    key = os.environ.get("ECHELON_API_KEY")
    if key:
        return key.strip()
    key = config.get("auth.api_key")
    if key:
        return str(key).strip()
    key = _read_named_key("ECHELON_API_KEY")
    if key:
        return key.strip()
    return None


def is_enabled() -> bool:
    """True when an API key IS configured → auth is active."""
    return get_api_key() is not None


async def require_auth(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_scheme),
) -> str:
    """FastAPI dependency: require a valid Bearer token matching ECHELON_API_KEY.

    Attach to any endpoint with `auth: str = Depends(require_auth)`. Returns
    the validated key on success (the endpoint can ignore it). If no key is
    configured anywhere, the dependency is a no-op (auth disabled).

    Raises HTTP 401 if a key IS configured and the request's Bearer token
    doesn't match.
    """
    configured = get_api_key()
    if configured is None:
        # Auth disabled — no key configured anywhere; allow all traffic.
        return ""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required (Bearer <ECHELON_API_KEY>)",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Constant-time-ish compare to avoid timing leaks (simple == is fine for
    # a shared secret of reasonable length in a local-trust context, but we
    # avoid early-exit on first mismatch just in case).
    if not _timing_safe_eq(credentials.credentials, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return credentials.credentials


def _timing_safe_eq(a: str, b: str) -> bool:
    """Constant-time string comparison.  Falls back to `==` if secrets not available."""
    try:
        import hmac
        return hmac.compare_digest(a.encode(), b.encode())
    except ImportError:
        return a == b
