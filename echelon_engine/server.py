"""server — `echelon-server`: single command to boot the ECHELON server side.

Starts the MCP proxy and the web dashboard as subprocesses. Handles SIGTERM
gracefully. Configuration via environment variables:

  ECHELON_PROXY_PORT  — proxy listen port (default: 18787)
  ECHELON_WEB_PORT    — web dashboard port (default: 8800)
  ECHELON_HOME        — bank + config root (default: ~/.echelon)
  ECHELON_SCOPE       — default bank scope (default: echelon)
  ECHELON_UPSTREAM    — upstream API for proxy (default: https://api.anthropic.com)

Usage:
  echelon-server                   # boot everything
  echelon-server --proxy-only      # proxy only
  echelon-server --web-only        # web dashboard only
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def _proxy_cmd(port: int = 18787) -> list[str]:
    """Build the proxy subprocess command."""
    return [
        sys.executable, "-X", "utf8", "-m", "echelon_engine", "proxy",
        "--port", str(port),
    ]


def _web_cmd(port: int = 8800) -> list[str]:
    """Build the web dashboard subprocess command."""
    return [
        sys.executable, "-X", "utf8", "-m", "uvicorn",
        "apps.web.app:make_app", "--factory",
        "--host", "0.0.0.0", "--port", str(port),
    ]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="echelon-server",
        description="Boot the ECHELON server side (proxy + web dashboard).")
    ap.add_argument("--proxy-only", action="store_true",
                    help="Start only the MCP proxy")
    ap.add_argument("--web-only", action="store_true",
                    help="Start only the web dashboard")
    ap.add_argument("--proxy-port", type=int,
                    default=int(os.environ.get("ECHELON_PROXY_PORT", "18787")),
                    help=f"Proxy port (default: $ECHELON_PROXY_PORT or 18787)")
    ap.add_argument("--web-port", type=int,
                    default=int(os.environ.get("ECHELON_WEB_PORT", "8800")),
                    help=f"Web port (default: $ECHELON_WEB_PORT or 8800)")
    a = ap.parse_args(argv)

    # Ensure the bank home exists
    home = Path(os.environ.get("ECHELON_HOME", Path.home() / ".echelon"))
    home.mkdir(parents=True, exist_ok=True)

    procs: list[subprocess.Popen] = []
    running = True

    def _shutdown(signum, frame):
        nonlocal running
        running = False
        print(f"\nechelon-server: received signal {signum}, shutting down...")

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        # ── Proxy ─────────────────────────────────────────────────────────
        if not a.web_only:
            cmd = _proxy_cmd(a.proxy_port)
            print(f"echelon-server: starting proxy on :{a.proxy_port}")
            proc = subprocess.Popen(
                cmd,
                stdout=sys.stdout, stderr=sys.stderr,
                cwd=str(Path(__file__).parent.parent),
            )
            procs.append(("proxy", proc))
            time.sleep(0.5)  # let it bind

        # ── Web dashboard ──────────────────────────────────────────────────
        if not a.proxy_only:
            cmd = _web_cmd(a.web_port)
            print(f"echelon-server: starting web dashboard on :{a.web_port}")
            proc = subprocess.Popen(
                cmd,
                stdout=sys.stdout, stderr=sys.stderr,
                cwd=str(Path(__file__).parent.parent),
            )
            procs.append(("web", proc))

        print("echelon-server: all services started. Ctrl+C to stop.")

        # ── Wait ───────────────────────────────────────────────────────────
        while running:
            for name, proc in procs:
                ret = proc.poll()
                if ret is not None:
                    print(f"echelon-server: {name} exited with code {ret}")
                    running = False
                    break
            time.sleep(1)

    finally:
        # ── Graceful shutdown ──────────────────────────────────────────────
        print("echelon-server: stopping services...")
        for name, proc in reversed(procs):
            print(f"  stopping {name}...")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                print(f"  {name} didn't stop, killing...")
                proc.kill()
                proc.wait()
        print("echelon-server: stopped.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
