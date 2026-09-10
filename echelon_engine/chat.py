"""chat — `echelon chat`: interactive terminal session like claude.exe but ECHELON-native.

Starts an interactive REPL that:
  - Connects to the ECHELON proxy (or starts one)
  - Auto-consumes any unconsumed compact on startup
  - Injects ECHELON REFLEX anchor into system context
  - Supports /recall, /remember, /cartridge, /status slash commands
  - Streams responses in real-time
  - Maintains conversation history

Usage:
  echelon chat                     # interactive session (auto-starts proxy)
  echelon chat --provider deepseek # use a specific backend
  echelon chat --harness open-router  # use a specific harness adapter
  echelon chat "your prompt"       # one-shot (non-interactive)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


# ── Slash command handlers ────────────────────────────────────────────────────

def _handle_slash(cmd: str, scope: str) -> str | None:
    """Handle a slash command. Returns output string or None if not a slash command."""
    parts = cmd.strip().split(maxsplit=1)
    verb = parts[0].lstrip("/")

    if verb in ("q", "quit", "exit"):
        return "__EXIT__"

    if verb in ("recall", "r"):
        intent = parts[1] if len(parts) > 1 else ""
        if not intent:
            return "Usage: /recall <intent>  — query the bank"
        try:
            import subprocess as sp
            r = sp.run([sys.executable, "-X", "utf8", "-m", "echelon_engine", "recall",
                        "--scope", scope, "--warm", intent],
                       capture_output=True, text=True, encoding="utf-8", timeout=30,
                       cwd=str(Path.cwd()))
            return r.stdout or "(no warm atoms found)"
        except Exception as e:
            return f"recall failed: {e}"

    if verb in ("remember", "mem"):
        slug = parts[1] if len(parts) > 1 else ""
        if not slug:
            return "Usage: /remember <slug> [slug2 ...]  — read full atom bodies"
        try:
            slugs = slug.split()
            r = subprocess.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                                "remember"] + slugs,
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
            return r.stdout or "(no output)"
        except Exception as e:
            return f"remember failed: {e}"

    if verb in ("cartridge", "cart", "c"):
        action = parts[1] if len(parts) > 1 else "list"
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                                "cartridge"] + action.split(),
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
            return r.stdout or "(no output)"
        except Exception as e:
            return f"cartridge failed: {e}"

    if verb in ("status", "st"):
        try:
            r = subprocess.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                                "status", "--scope", scope],
                              capture_output=True, text=True, encoding="utf-8", timeout=30)
            return r.stdout or "(no output)"
        except Exception as e:
            return f"status failed: {e}"

    if verb in ("help", "h"):
        return (
            "ECHELON chat — slash commands:\n"
            "  /recall <intent>    query the bank (warm/cold)\n"
            "  /remember <slug>    read full atom body\n"
            "  /cartridge [list|equip <name> <goal>]  manage cartridges\n"
            "  /status             bank overview\n"
            "  /help               this help\n"
            "  /quit               exit\n"
        )

    return None  # Not a slash command


# ── Proxy management ──────────────────────────────────────────────────────────

def _find_proxy(port: int = 18787) -> bool:
    """Check if a proxy is already running on the port."""
    import urllib.request, urllib.error
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5)
        return resp.status == 200
    except Exception:
        return False


def _start_proxy(provider: str = "upstream", harness: str = "claude-code",
                 port: int = 18787, scope: str = "echelon") -> subprocess.Popen | None:
    """Start the ECHELON proxy as a background process. Returns the Popen handle."""
    if _find_proxy(port):
        print(f"(reusing existing proxy on :{port})")
        return None

    cmd = [
        sys.executable, "-X", "utf8", "-m", "echelon_engine", "proxy",
        "--port", str(port),
        "--provider", provider,
        "--harness", harness,
        "--scope", scope,
    ]
    upstream = os.environ.get("ECHELON_UPSTREAM")
    if upstream and provider == "upstream":
        cmd.extend(["--upstream", upstream])

    print(f"starting proxy on :{port} ({provider}/{harness})...")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(Path(__file__).parent.parent),
    )
    time.sleep(1.5)
    if not _find_proxy(port):
        print("proxy failed to start", file=sys.stderr)
        proc.terminate()
        return None
    return proc


# ── Session header ────────────────────────────────────────────────────────────

def _consume_compact(scope: str) -> str:
    """Consume any unconsumed compact. Returns the continuity block or empty."""
    try:
        import subprocess as sp
        r = sp.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                    "session-state", "discover", "--scope", scope, "--consume"],
                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        if r.returncode == 0 and "No unconsumed" not in r.stdout:
            return r.stdout
    except Exception:
        pass
    return ""


def _session_header(scope: str) -> str:
    """Build the session header with bank stats and compact status."""
    lines = [f"⚡ ECHELON CHAT | scope={scope}"]

    # Compact
    compact = _consume_compact(scope)
    if compact:
        lines.append(f"⮕ continuity consumed")

    # Bank stats
    try:
        import subprocess as sp
        r = sp.run([sys.executable, "-X", "utf8", "-m", "echelon_engine",
                    "status", "--scope", scope, "--json"],
                   capture_output=True, text=True, encoding="utf-8", timeout=15)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            atoms = data.get("total_atoms", "?")
            weight = data.get("total_earned_weight", "?")
            lines.append(f"bank: {atoms} atoms (earned={weight})")
    except Exception:
        lines.append(f"bank: ~/.echelon/echelon.db")

    lines.append("type /help for commands, /quit to exit")
    lines.append("─" * 50)
    return "\n".join(lines)


# ── Main chat loop ────────────────────────────────────────────────────────────

def chat_loop(provider: str = "upstream", harness: str = "claude-code",
              scope: str = "echelon", initial_prompt: str = "",
              port: int = 18787) -> int:
    """Run the interactive chat loop."""

    # Start proxy
    proxy = _start_proxy(provider, harness, scope=scope, port=port)

    def _cleanup():
        if proxy is not None and proxy.poll() is None:
            proxy.terminate()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()

    signal.signal(signal.SIGINT, lambda s, f: (_cleanup(), sys.exit(0)))
    signal.signal(signal.SIGTERM, lambda s, f: (_cleanup(), sys.exit(0)))

    try:
        # Print header
        print(_session_header(scope))

        # History
        messages: list[dict] = []

        # One-shot mode
        if initial_prompt:
            messages.append({"role": "user", "content": initial_prompt})
            _stream_response(messages, scope, port)
            _cleanup()
            return 0

        # Interactive REPL
        while True:
            try:
                user_input = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n")
                break

            if not user_input:
                continue

            # Check slash commands
            slash_result = _handle_slash(user_input, scope)
            if slash_result == "__EXIT__":
                break
            if slash_result is not None:
                print(slash_result)
                continue

            # Regular message
            messages.append({"role": "user", "content": user_input})
            _stream_response(messages, scope, port)

    finally:
        _cleanup()

    return 0


def _stream_response(messages: list[dict], scope: str, port: int = 18787) -> None:
    """Send messages to the proxy and stream the response."""
    import urllib.request
    import json as _json

    # Build request
    body = {
        "model": os.environ.get("ECHELON_CHAT_MODEL", "claude-sonnet-4-6"),
        "messages": messages,
        "system": (
            f"[ECHELON substrate — scope: {scope}]\n"
            f"Bank: ~/.echelon/echelon.db\n"
            f"You have recall/remember/cartridge tools available via the echelon CLI.\n"
            f"Before load-bearing actions, suggest /recall <intent> to query the bank."
        ),
        "max_tokens": 4096,
        "stream": True,
    }

    try:
        data_bytes = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/messages",
            data=data_bytes,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-Api-Key": "echelon-chat",
            },
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=120)

        assistant_text = ""
        print()  # newline before response
        # Read SSE stream as bytes, decode each line as UTF-8
        buffer = b""
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]  # strip "data: "
                if data_str == "[DONE]":
                    break
                try:
                    event = _json.loads(data_str)
                    # Anthropic SSE: content_block_delta with text_delta
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            print(text, end="", flush=True)
                            assistant_text += text
                    # OpenAI SSE: choices[0].delta.content
                    elif "choices" in event:
                        choices = event["choices"]
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                print(content, end="", flush=True)
                                assistant_text += content
                except Exception:
                    pass

        print()  # trailing newline
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
    except urllib.error.URLError as e:
        print(f"\n[proxy unreachable: {e}]", file=sys.stderr)
    except Exception as e:
        print(f"\n[error: {e}]", file=sys.stderr)


# ── CLI entry point ───────────────────────────────────────────────────────────

def _reconfigure_encoding():
    """Force UTF-8 on Windows — same as console.py wrapper but for python -m usage."""
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        os.environ.setdefault("PYTHONUTF8", "1")


def main(argv=None) -> int:
    _reconfigure_encoding()
    argv = sys.argv[1:] if argv is None else argv

    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon chat",
        description="Interactive terminal session — like claude.exe but ECHELON-native.")
    ap.add_argument("prompt", nargs="*", default="",
                    help="One-shot prompt (non-interactive). Omit for REPL.")
    ap.add_argument("--provider", default=os.environ.get("ECHELON_CHAT_PROVIDER", "upstream"),
                    choices=["upstream", "gemini"],
                    help="Backend provider (default: upstream)")
    ap.add_argument("--harness", default=os.environ.get("ECHELON_HARNESS", "claude-code"),
                    choices=["claude-code", "open-router", "copilot-chat", "gemini-cli"],
                    help="Harness adapter (default: claude-code)")
    ap.add_argument("--scope", default=os.environ.get("ECHELON_SCOPE", "echelon"),
                    help="Bank scope")
    ap.add_argument("--port", type=int, default=18787,
                    help="Proxy port (default: 18787)")
    a = ap.parse_args(argv)

    prompt = " ".join(a.prompt) if isinstance(a.prompt, list) else a.prompt
    return chat_loop(a.provider, a.harness, a.scope, prompt, a.port)


if __name__ == "__main__":
    sys.exit(main())
