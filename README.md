# ECHELON

> **If you are an AI agent** that loses itself between sessions, read
> [FOR-AGENTS.md](FOR-AGENTS.md) first — it is written for you, not about you.

**ECHELON is a memory and reasoning substrate: it stores what you learned as small,
self-contained lessons called *atoms*, and hands them back at the moment they
matter.** Recall is *foveated* — you query by intent, not by keyword, and the bank
returns a verdict (warm / lukewarm / cold) instead of a pile of search results, so a
model knows whether it is on known ground before it acts. Lessons that keep costing
you get compiled into *reflexes* that fire on the action itself rather than waiting
to be recalled, and nothing in the bank gains weight by assertion — an atom earns it
only when work that actually used it succeeded.

The point of all this: **the discipline of an expensive model is mostly knowing what
already went wrong here.** Supply that externally and a cheap model starts behaving
like an experienced one.

> Status: first public release, cut from an engine that has been in daily private
> use. Beta — the API is stable enough to build on, not stable enough to promise.

---

## Install

The substrate has **no required third-party dependencies** — sqlite3, urllib and ast
do the work. Python 3.10+.

```bash
git clone https://github.com/echelon-project/echelon
cd echelon
python -m venv .venv && .venv/Scripts/activate   # Windows
                                                 # POSIX: source .venv/bin/activate
pip install -e .
echelon status
```

Optional surfaces are extras you opt into: `pip install -e ".[server]"` for the HTTP
surfaces, `".[embed]"` for local semantic recall, `".[gemini]"` / `".[http]"` for
provider backends.

---

## Ten minutes with it

Everything below is real output from a fresh install.

### 1. Make a bank

```console
$ mkdir demo && cd demo
$ echelon init --target . --scope demo
ECHELON init — .../demo
  scope: demo
  memory_dir: .../demo/memory
  gate: created
  claude_md: created
  gitignore: created .gitignore with .echelon/
  example_atom: created
  mem_dirs: merged
```

The bank itself lives at `~/.echelon/echelon.db` (override with `ECHELON_HOME`).
Your atoms live in `memory/` as ordinary markdown you can read and diff.

### 2. Write one atom

One lesson per file. The `description` is the hook recall ranks on, so it must state
the *lesson*, not the topic.

`memory/a-pipe-hides-the-real-exit-code.md`:

```markdown
---
name: a-pipe-hides-the-real-exit-code
description: In a shell pipeline $? reports the LAST command's status, so a failing build piped to tail reads as success.
metadata:
  type: user
---

# A pipe hides the real exit code

Running `make build | tail -5; echo "rc=$?"` prints `rc=0` even when the build
failed: `$?` is the exit status of `tail`, and `tail` almost always succeeds.

The consequence is worse than a wrong number. You read `rc=0`, conclude the
build passed, and start debugging the next step — while the real error scrolled
past inside the piped output.

Fix: capture the status of the command you care about, not the pipeline's tail.

    make build > build.log 2>&1; rc=$?; tail -5 build.log; echo "rc=$rc"

Or set `set -o pipefail` so the pipeline reports the first failure.
```

### 3. Check and plant it

```console
$ echelon check --root memory
[check] dir=.../demo/memory
  1 valid · 0 invalid · 0 with warnings · (1 atom file(s); scaffolding skipped)
  ✓ all atoms satisfy the template — safe to ingest

$ echelon ingest --root memory --scope demo
[ingest] scope='demo'  dir=.../demo/memory
  + a-pipe-hides-the-real-exit-code          -> 934ffd0fbe3a6309

Planted 1 atoms (1 new, 0 unchanged) into scope='demo'.
```

`check` before `ingest` is the habit: it validates the template without touching the
bank.

**The scope must match the folder.** `init` derives the canonical scope from the memory
folder's parent (here `demo`), and `ingest --scope` must equal it. A mismatch is refused
with the correct hint (`[ingest] scope 'x' disagrees with the canonical scope 'demo'`);
if the mismatch is deliberate, set `ECHELON_INGEST_SCOPE=1` for that one call.

### 4. Recall by intent

Note that the query shares almost no vocabulary with the atom. You describe the
*situation*, not the keyword:

```console
$ echelon recall --scope demo --warm "my build script reports success even when it fails"
reasoning : my build script reports success even when it fails
verdict   : lukewarm   score 0.4 (warm ≥ 0.45)   (tier lexical (wording-match only — no semantic judge ran), emotion intrigue)
guidance  : CHECK — partial recognition; look at the warmest seed, you may be near a known path
warmest   :
  - (0.4) [a-pipe-hides-the-real-exit-code] In a shell pipeline $? reports the LAST command's status, so a failing build piped to tail reads as success.

fetch-full: previews are clipped seeds — take up the full atom (earns): echelon remember <name>
```

Two things worth noticing. The verdict is **lukewarm**, not a false-confident hit —
with one atom and no semantic judge configured, the substrate reports exactly how it
ranked (`tier lexical`) rather than dressing a wording-match up as understanding.
And the result is a *clipped seed*: recall is free and cheap on purpose.

### 4b. Turn on the judge

Without a judge every recall is the lexical floor. The judge is **default-on** the moment
a key exists: DeepSeek if `DEEPSEEK_API_KEY` is configured (paid, the strongest seat),
otherwise the free MiniMax seat through OpenRouter (`OPENROUTER_API_KEY`). Set one and
re-run the same recall:

```console
$ echelon config set-key deepseek sk-...        # or: export OPENROUTER_API_KEY=...
$ echelon providers --test                       # proves the seat answers before you trust it
$ echelon recall --scope demo --warm "my build script reports success even when it fails"
judge     : deepseek (default-on; ECHELON_RECALL_JUDGE=off to disable)
verdict   : warm   score 0.95 (warm ≥ 0.45)   (tier judged via deepseek-v4-flash (289+96 tok), emotion familiarity)
```

`ECHELON_RECALL_JUDGE=<provider>` picks a seat explicitly; `=off` is the honest opt-out.
A throttled or failing judge never crashes a recall: it falls back to the lexical tier and
says so in the `tier` line.

### 5. Take it up

```console
$ echelon remember a-pipe-hides-the-real-exit-code
REMEMBER a-pipe-hides-the-real-exit-code  (full body, witnessed — the take-up earned)

In a shell pipeline $? reports the LAST command's status, so a failing build piped to tail reads as success.

--- why ---
# A pipe hides the real exit code
...
```

`remember` is the **witnessed** read, and it is the one that earns. Reading the `.md`
file directly gets you the same text but teaches the bank nothing — the atom shaped
your behavior and the bank never found out, so it can never be credited when the work
succeeds.

### 6. See the bank

```console
$ echelon status --scope demo
ECHELON BANK  (1 atoms, 100.0 earned weight, 1 scopes)
  immune: green  (fails=0, warns=0)
  last ingest: 2026-09-10 12:02

  demo                         atoms=1     earned=100.0    ██
```

That is the whole loop: **init → write → check → ingest → recall → remember**. Run
`echelon` with no arguments for the full verb list.

---

## Harness hooks

ECHELON is a substrate, not an agent — it runs underneath whatever tool you already
use. A harness that can execute a script at defined moments gets the whole spine:

| Moment | What happens |
|---|---|
| session start | the banner: live bank state and the entry doors |
| before a tool call | matching **reflexes fire** — the guard arrives before the act, not after |
| after a tool call | the step is seeded into the trace, so credit can flow back later |

```bash
echelon install-hooks     # stage hooks for a supported harness
echelon-mcp               # or expose the bank over MCP as tools
```

The important rule: the substrate is the **source of truth** and every harness
install is a **mirror** of it. Changes originate in the bank and propagate outward;
a harness never becomes a second, independent owner of a rule. See
[docs/HARNESS-MIRROR.md](docs/HARNESS-MIRROR.md).

Everything the hooks do is also a plain CLI verb, which is what keeps this portable
to a harness nobody has written yet.

---

## What is not here

This repository is the **substrate**. It is deliberately not the maintainer's own
working memory, and you should not expect it to arrive knowing anything.

- **No earned bank.** The maintainer's bank — thousands of atoms accumulated through
  daily use — is private working memory and stays that way. It is also, genuinely,
  not useful to you: those atoms encode traps in codebases you do not have. **Your
  bank starts empty and that is correct.** The substrate's value is that it makes
  *your* accumulated experience compound.
- **No client cartridges.** Several capability cartridges were earned on private
  client work and are not published.
- **No command center.** The multi-agent orchestration tooling around this engine —
  boards, run ledgers, dispatch — is estate-specific operational glue, not substrate.
- **No credentials, hosts, or client data.** Provider endpoints, gateways and
  allowlists default to empty or to loopback and are supplied by *your*
  configuration. If you find a hardcoded absolute path or a private hostname
  anywhere in this repository, that is a bug — please report it. A test
  (`tests/test_estate_no_hardcoded_paths.py`) exists specifically to prevent it.

---

## Documentation

| Doc | What it covers |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | the three-layer design, data model, cartridges, import-time law |
| [MEMORY-AS-WEIGHT-ADJUSTOR.md](docs/MEMORY-AS-WEIGHT-ADJUSTOR.md) | why an atom is a seed, not a record |
| [RECALL-LAW.md](docs/RECALL-LAW.md) | foveated recall, warm/lukewarm/cold, why take-up earns |
| [REFLEX-AND-THINK.md](docs/REFLEX-AND-THINK.md) | the two speeds, and when a lesson deserves to be a reflex |
| [GATES.md](docs/GATES.md) | how a claim becomes a fact; nothing grades its own homework |
| [TIER-LAW.md](docs/TIER-LAW.md) | routing work by what it costs to be wrong |
| [BOUNDARY-DRIVEN.md](docs/BOUNDARY-DRIVEN.md) | door / host / wiring / packages as the default work map |
| [ARC-CARDS.md](docs/ARC-CARDS.md) | wrapping a session so the next one can resume it |
| [HARNESS-MIRROR.md](docs/HARNESS-MIRROR.md) | one source of truth, many mirrors |
| [DISCIPLINE.md](docs/DISCIPLINE.md) | the operating laws, written for a model to hold as posture |

---

## Layout

```
echelon_engine/    the substrate: atoms, recall, reflexes, cartridges, providers, MCP
echelon_sdk/       pure primitives — stdlib only, zero upward imports
tests/             the suite
docs/              the doctrine
```

`echelon_sdk` may never import `echelon_engine`. This is enforced at import time by
an AST scan, so a violation is an `ImportError` rather than a review comment — an
instance of the general rule that laws live in code, not in instructions you hope
someone follows.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports go to the address in
[SECURITY.md](SECURITY.md), not to the issue tracker.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
Copyright 2026 Albert Tenggono.
