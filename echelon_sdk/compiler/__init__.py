"""echelon_sdk.compiler — the compiler subsystem.

The compiler is the standalone single writer: it writes files directly (pathlib
Path.write_text / diff-apply), NOT through the agent's ToolRegistry. The swarm's
hands no longer write; only the compiler does.
"""
