# Security Policy

## Reporting a vulnerability

Please report security issues privately to **albertteng78@gmail.com**.
Do not open a public issue for a vulnerability.

Include, as far as you can:

- what the issue is and which component it affects (engine, bank, MCP server, proxy),
- the version or commit you tested,
- steps to reproduce, and
- what an attacker gains.

You should get an acknowledgement within 7 days. Once a fix is available it will
be released and the report credited, unless you prefer otherwise.

## Scope notes

ECHELON is a **local-first** substrate. The bank is a SQLite file under
`~/.echelon/`, and the default surfaces bind to loopback. Two areas deserve
particular care, and reports about them are especially welcome:

- **Bank integrity** — anything that lets untrusted input write, dispute, or
  re-weight atoms it should not, or that escapes an atom's declared scope.
- **The network surfaces** — the MCP server, the API proxy, and the agent
  bridge. They are loopback-bound by default; if you expose one, treat it as an
  authenticated service. A path that reaches raw CLI argv from an unprivileged
  caller is a vulnerability, not a feature.

Provider API keys are read from the environment or from files you control;
ECHELON never transmits them anywhere except to the provider they belong to.
