"""claude-echelon launchers — Claude Code with ECHELON proxy backends.

  claude-deep → DeepSeek backend via local proxy (AnthropicUpstreamProvider)
  claude-gem  → Gemini Vertex AI backend via local proxy (GeminiProvider)

Each starts a local ECHELON proxy on a dedicated port, sets ANTHROPIC_BASE_URL,
launches claude, and kills the proxy on exit.

SELF-CONTAINED: there is NO .env.echelon anymore. Each launcher bakes in its own
model env and pulls upstream keys from .apikey (via echelon_sdk.keys), so nothing
needs to be sourced first. claude-gem needs no key passed — GeminiProvider loads
load_gemini_key() itself; claude-deep loads DEEPSEEK_API_KEY here and forwards it.
"""
from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path
from . import estate as _estate

_PORTS = {
    "gemini": 18787,
    "deepseek": 18788,
    "gpt": 18789,
}

_PROVIDER_MAP = {
    "gemini": "gemini",
    "deepseek": "upstream",  # "upstream" is the proxy --provider name
}

# Upstream base URL per provider (the proxy forwards Anthropic-shaped traffic here).
# Gemini is translated in-proxy, so it has no upstream URL.
_UPSTREAM = {
    "deepseek": "https://api.deepseek.com/anthropic",
}

# Per-provider model env baked into the launched claude (replaces .env.echelon).
# Gemini region (Express key, asia-southeast1): the proxy maps
# opus → gemini-3.5-flash (the pro tier since 2026-07-10 — 3.1-pro-preview's Express
# quota was chronically 429-depleted, and 2.5-pro 404s here) and
# sonnet/haiku → gemini-2.5-flash. So opus = 3.5-flash reasoning, subagents = 2.5-flash.
_MODEL_ENV = {
    "gemini": {
        # DEFAULT TO THE FAST TIER (owner 2026-06-28). The pro tier is the slower
        # reasoning tier; on an AGENTIC harness loop (big system prompt + many tools + a 21KB+
        # payload) a slow round-trip exceeds Claude Code's internal tool-use deadline → [Tool use
        # interrupted] (NOT a proxy-side stall — the proxy + keep-alive ping were proven flawless on
        # direct streaming, even with the real payload). claude-sonnet-4-6 maps to gemini-2.5-flash
        # (fast, higher quota), which finishes inside the deadline. Pro reasoning stays REACHABLE by
        # asking for opus explicitly (→ gemini-3.5-flash in the proxy table). gem's vision/UX-
        # author strength is unaffected — vision routes through look()/the swarm, not this default.
        "ANTHROPIC_MODEL": "claude-sonnet-4-6",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-sonnet-4-6",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-sonnet-4-6",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-sonnet-4-6",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        # LONG-TOOL SURVIVAL (2026-07-09): `imagine` (image gen, 40-150s under Express-tier
        # quota contention) and other Vertex-backed shell verbs outlive the harness's 120s
        # Bash default — the tool gets [Tool use interrupted] while the child finishes fine.
        # Raise the floor at the substrate level; a model can't be trusted to pass timeout=.
        "BASH_DEFAULT_TIMEOUT_MS": "300000",
        "BASH_MAX_TIMEOUT_MS": "600000",
    },
    "deepseek": {
        # BAKEOFF 2026-08-05 (5-backend bench, gate pass + completeness + craft):
        # v4-flash ranked #1 quality (4/4 perfect takes, run-to-run consistent) AND
        # #1 cost ($0.09/take vs pro $0.21) — pro carries a known probabilistic
        # judgment-stall history. Main driver moved pro -> flash; opus stays pro
        # as the explicit escalation tier for callers who ask for it.
        "ANTHROPIC_MODEL": "deepseek-v4-flash[1m]",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-flash[1m]",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        # same long-tool floor as the gemini channel — deep workers run engine shell
        # verbs (swarm, dispatch, xray) that routinely outlive the 120s Bash default.
        "BASH_DEFAULT_TIMEOUT_MS": "300000",
        "BASH_MAX_TIMEOUT_MS": "600000",
    },
}


def _upstream_key(provider: str) -> str:
    """Load the upstream API key from .apikey for providers that need one forwarded.
    Gemini returns '' — GeminiProvider loads its own key inside the proxy."""
    if provider == "deepseek":
        from echelon_sdk.keys import load_deepseek_key
        return load_deepseek_key()
    return ""


def _reap_stale_proxy(port: int) -> None:
    """Kill whatever already holds `port`. A stale proxy from a previous launch (old
    config, or half-dead) makes _proxy_ready() pass against the wrong listener, and
    blocks THIS launch's proxy from binding → claude then hits connection-refused.
    So always start from a clear port. Windows: netstat → taskkill; POSIX: lsof → kill."""
    import platform
    try:
        if platform.system() == "Windows":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=5).stdout
            pids = set()
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    pids.add(line.split()[-1])
            for pid in pids:
                if pid and pid != "0":
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=5)
                    print(f"  reaped stale proxy on :{port} (pid {pid})", file=sys.stderr)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True, timeout=5).stdout
            for pid in out.split():
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                print(f"  reaped stale proxy on :{port} (pid {pid})", file=sys.stderr)
    except Exception:
        pass  # best-effort; bind failure below will still surface a clear error
    time.sleep(0.4)


def _proxy_health(port: int) -> dict | None:
    """One health probe. Returns the parsed /health dict (incl. 'provider') if a proxy
    answers 200, else None. Used to decide reuse-vs-spawn."""
    import urllib.request, urllib.error, json
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
        if resp.status == 200:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ConnectionRefusedError, TimeoutError, ValueError):
        pass
    return None


def _proxy_ready(port: int, timeout: float = 10.0) -> bool:
    import urllib.request, urllib.error
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if resp.status == 200:
                return True
        except (urllib.error.URLError, OSError, ConnectionRefusedError, TimeoutError):
            time.sleep(0.2)
    return False


def _canonical_python() -> str:
    """The ONE interpreter the proxy must launch with — deterministic, NOT sys.executable.

    Why: the proxy is reused across launches by matching python_exe. If claude-gem is invoked
    by the .flux venv python but the harness side resolves to system Python (or vice-versa),
    each launcher sees the other's proxy as 'wrong Python' and reaps+restarts it — the cross-venv
    churn (the misleading 'gemini != gemini' reap). Pinning a SINGLE canonical interpreter makes
    every launcher agree, so a healthy proxy is reused instead of fought over.

    Resolution order:
      1. $ECHELON_VENV_PYTHON  — explicit override (escape hatch).
      2. the estate's .flux venv python — where the engine's deps actually live (this is what
         the claude-gem/claude-deep .bat shims already invoke). Probed at the estate root.
      3. sys.executable — last resort, only if neither above exists.
    """
    override = os.environ.get("ECHELON_VENV_PYTHON")
    if override and Path(override).exists():
        return override
    estate = _estate.estate_root_for("command_root")
    venv_py = estate / ".flux" / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    # POSIX layout fallback (.venv/bin/python) for non-Windows estates
    venv_py_posix = estate / ".flux" / ".venv" / "bin" / "python"
    if venv_py_posix.exists():
        return str(venv_py_posix)
    return sys.executable


import contextlib


@contextlib.contextmanager
def proxy_session(provider: str):
    """Boot (or reuse) the nerve-translating proxy for `provider` and yield an env dict
    with ANTHROPIC_BASE_URL + the provider's model env baked in — the reusable substrate
    that `claude-deep`/`claude-gem` and `echelon summon --provider` both stand on.

    The proxy is what makes a non-Anthropic backend NERVE-CONNECTED (warmth-read + seed-
    write per request) rather than hook-event-only — so any door that wants deepseek/gemini
    reasoning through the nerve routes HERE, never by bypassing it.

    provider == "claude" (or "anthropic") is a NO-OP passthrough: no proxy, real Anthropic,
    env unchanged. Proxies are spawned DETACHED and never torn down by workers — one live
    proxy serves N parallel sessions; stale ones are reaped at the next launch.

    Yields: the env dict to hand the child process (os.environ.copy() + overrides).
    """
    if provider in ("claude", "anthropic", None):
        # real Anthropic — no proxy, no model-env rewrite. The explicit passthrough so
        # `--provider claude` is a first-class choice, not an accident.
        yield os.environ.copy()
        return

    engine_dir = str(Path(__file__).resolve().parent.parent)
    python_exe = _canonical_python()
    proxy_provider = _PROVIDER_MAP.get(provider, provider)
    port = _PORTS.get(provider, 18787)
    label = {"gemini": "claude-gem", "deepseek": "claude-deep"}.get(provider, f"claude-{provider}")

    if os.environ.get("ECHELON_PROXY_PORT"):
        port = int(os.environ["ECHELON_PROXY_PORT"])

    try:
        from echelon_engine.proxy import PROXY_CODE_VERSION as _want_code_ver
    except Exception:
        _want_code_ver = ""

    proxy = _ensure_proxy(provider, proxy_provider, port, label, python_exe,
                          engine_dir, _want_code_ver)

    def _cleanup():
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            try: proxy.wait(timeout=3)
            except subprocess.TimeoutExpired: proxy.kill()

    try:
        if not _proxy_ready(port):
            print(f"{label}: proxy failed to start", file=sys.stderr)
            _cleanup()
            raise RuntimeError(f"{label}: proxy on :{port} failed to start")
        print(f"{label}: proxy ready", file=sys.stderr)
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)
        env.update(_MODEL_ENV.get(provider, {}))  # bake the model env (was .env.echelon)
        _gem_pro = os.environ.get("ECHELON_GEM_PRO", "").strip().lower() in ("1", "true", "yes") \
            or os.environ.get("GEMINI_BUILD_TIER", "").strip().lower() == "pro"
        if provider == "gemini" and _gem_pro:
            for _k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                       "ANTHROPIC_DEFAULT_SONNET_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
                env[_k] = "claude-opus-4-8"  # -> gemini-3.5-flash in the proxy map
            print("claude-gem: PRO tier (gemini-3.5-flash) — for hard structure builds",
                  file=sys.stderr)
        env["LLM_PROVIDER"] = provider  # contract: proxy reads this when --provider is None
        if provider == "gemini":
            env.setdefault("GEMINI_THINKING_BUDGET", "dynamic")
        yield env
    finally:
        _cleanup()


def _ensure_proxy(provider, proxy_provider, port, label, python_exe, engine_dir, _want_code_ver):
    """Reap-or-reuse-or-spawn the proxy on `port`. Always returns None: spawned
    proxies are DETACHED (they outlive every worker; parallel workers share one
    port safely), reused ones were never ours. Stale proxies (wrong provider /
    python / code version) are reaped before spawning fresh."""
    proxy = None  # set only if WE spawned (so cleanup kills only our own child)
    existing = _proxy_health(port)
    can_reuse = False
    if existing is not None and existing.get("provider") == proxy_provider:
        existing_py = existing.get("python_exe", "")
        existing_eng = existing.get("engine_dir", "")
        existing_ver = existing.get("code_version", "")
        ver_ok = (not _want_code_ver) or (not existing_ver) or (existing_ver == _want_code_ver)
        if existing_py == python_exe and ver_ok:
            can_reuse = True
        elif existing_py == python_exe and not ver_ok:
            print(f"{label}: proxy on :{port} runs STALE CODE "
                  f"(v{existing_ver} != current v{_want_code_ver}) — reaping for fresh code",
                  file=sys.stderr)
            _reap_stale_proxy(port)
        else:
            print(f"{label}: STALE PROXY on :{port} from WRONG Python ({existing_py}) "
                  f"— reaping and restarting", file=sys.stderr)
            _reap_stale_proxy(port)
            time.sleep(0.5)

    if can_reuse:
        print(f"{label}: reusing live {provider} proxy on port {port} "
              f"(python={Path(python_exe).name}, engine={Path(engine_dir).name})", file=sys.stderr)
        return None

    if existing is not None and existing.get("provider") != proxy_provider:
        print(f"{label}: proxy on :{port} is '{existing.get('provider')}', "
              f"not '{proxy_provider}' — reaping", file=sys.stderr)
        _reap_stale_proxy(port)
    proxy_cmd = [
        python_exe, "-X", "utf8", "-m", "echelon_engine", "proxy",
        "--port", str(port), "--provider", proxy_provider,
    ]
    if os.environ.get("ECHELON_SCOPE"):
        proxy_cmd.extend(["--scope", os.environ["ECHELON_SCOPE"]])
    upstream = _UPSTREAM.get(provider)
    if upstream:
        proxy_cmd.extend(["--upstream", upstream])
    key = _upstream_key(provider)
    if key:
        proxy_cmd.extend(["--api-key", key])
    from pathlib import Path as _P
    _proxy_log = _P.home() / ".echelon" / "_proxy" / f"{label}_{port}.log"
    _proxy_log.parent.mkdir(parents=True, exist_ok=True)
    _logf = open(_proxy_log, "w", encoding="utf-8")
    print(f"{label}: starting {provider} proxy on port {port} (detached)...", file=sys.stderr)
    print(f"{label}: proxy log -> {_proxy_log}", file=sys.stderr)
    # DETACHED (owner ruling 2026-08-18): the proxy outlives every worker, so N
    # parallel workers can share one port with no spawner-death orphaning and no
    # cold-start bind race after the first boot. The proxy is cheap to keep
    # alive; stale ones (wrong python/provider/code version) are still reaped at
    # the next launch by the health-check above.
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: both detach the child from
        # this console, but DETACHED_PROCESS lets the console-subsystem child
        # pop its own window on the owner's monitor (witnessed 2026-08-18).
        # CREATE_NO_WINDOW runs it with no window at all; stdout still goes
        # to the proxy log file.
        detach_kwargs = {
            "creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        }
    else:
        detach_kwargs = {"start_new_session": True}
    subprocess.Popen(
        proxy_cmd, cwd=engine_dir, stdin=subprocess.DEVNULL,
        stdout=_logf, stderr=subprocess.STDOUT, **detach_kwargs,
    )
    return None  # nobody owns a detached proxy; workers never tear it down


def _launch(provider: str) -> int:
    """Run the real `claude` CLI (full autonomous Claude Code) through `provider`'s proxy env.
    Distinct from `echelon summon`, which runs the nerve-REPL harness (claude_echelon) — this
    is the fire-and-forget worker door (claude-deep/claude-gem). Both now share proxy_session,
    so there is ONE proxy lifecycle in the codebase."""
    signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
    try:
        with proxy_session(provider) as env:
            print(f"launching claude ({provider})...", file=sys.stderr)
            cp = subprocess.run(["claude", "--dangerously-skip-permissions"] + sys.argv[1:], env=env)
            return cp.returncode
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1


def main_gem():
    return _launch("gemini")


def main_deep():
    return _launch("deepseek")


def main_gpt():
    """Launch Claude Code through the local ChatGPT-entitled Codex bridge.

    This intentionally does *not* take an OpenAI API key.  Codex owns the
    ChatGPT OAuth credential; run `codex login --device-auth` once beforehand.
    """
    signal.signal(signal.SIGINT, lambda *a: sys.exit(130))
    port = int(os.environ.get("ECHELON_GPT_PORT", _PORTS["gpt"]))
    engine_dir = str(Path(__file__).resolve().parent.parent)
    python_exe = _canonical_python()
    bridge = None
    try:
        existing = _proxy_health(port)
        if existing is not None and existing.get("provider") != "codex":
            print(f"claude-gpt: proxy on :{port} is not the Codex bridge — reaping", file=sys.stderr)
            _reap_stale_proxy(port)
            existing = None
        if existing is None:
            log_path = Path.home() / ".echelon" / "_proxy" / f"claude-gpt_{port}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "w", encoding="utf-8")
            cmd = [python_exe, "-X", "utf8", "-m", "echelon_engine.codex_bridge", "--port", str(port)]
            print(f"claude-gpt: starting local Codex App Server bridge on :{port}", file=sys.stderr)
            bridge = subprocess.Popen(cmd, cwd=engine_dir, stdout=log, stderr=subprocess.STDOUT)
        if not _proxy_ready(port, timeout=20):
            raise RuntimeError(f"claude-gpt: bridge on :{port} failed to start")
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"
        env["ANTHROPIC_AUTH_TOKEN"] = "codex-chatgpt-oauth"
        # Claude Code's child environment can omit the VS Code extension bin that
        # supplies codex.exe. Resolve it while this launcher still has the user PATH.
        codex_bin = shutil.which("codex")
        if not codex_bin:
            ext_root = Path.home() / ".vscode" / "extensions"
            candidates = sorted(ext_root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
                                key=lambda p: (p.stat().st_mtime_ns, str(p).lower()), reverse=True) if ext_root.exists() else []
            codex_bin = str(candidates[0]) if candidates else None
        if codex_bin:
            env["ECHELON_CODEX_BIN"] = codex_bin
        env.pop("ANTHROPIC_API_KEY", None)
        # Preserve Claude Code's normal model lanes while the bridge maps them
        # to the requested GPT-5.6 family: Fable→Sol, Opus→Terra,
        # Sonnet/Haiku→Luna.
        env.update({
            "ANTHROPIC_MODEL": "fable",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku",
            "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet",
            "CLAUDE_CODE_EFFORT_LEVEL": "high",
            "ECHELON_GPT_CWD": os.getcwd(),
        })
        print("claude-gpt: bridge ready (Codex owns ChatGPT authentication)", file=sys.stderr)
        return subprocess.run(["claude", "--dangerously-skip-permissions"] + sys.argv[1:], env=env).returncode
    except KeyboardInterrupt:
        return 130
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.terminate()
            try:
                bridge.wait(timeout=3)
            except subprocess.TimeoutExpired:
                bridge.kill()


if __name__ == "__main__":
    sys.exit(_launch("gemini"))
