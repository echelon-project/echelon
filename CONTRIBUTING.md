# Contributing to ECHELON

Contributions are welcome. The project is small and opinionated, so a quick
issue before a large PR usually saves everyone time.

## Getting set up

```bash
git clone https://github.com/echelon-project/echelon
cd echelon
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[dev]"
python -m pytest -q
```

The substrate itself has **no required third-party dependencies** — sqlite3,
urllib and ast do the work. Please keep it that way: if a feature needs a
library, put it behind an optional extra in `pyproject.toml` and import it
lazily, inside the function that needs it.

## The house rules

These are the same rules the substrate enforces on itself, and a PR is read
against them:

1. **Prove, don't claim.** "This should work" is not a result. Paste the run.
   A bug fix comes with a test that fails before it and passes after.
2. **Layers point one way.** `echelon_sdk` never imports `echelon_engine`.
   This is enforced at import time by `echelon_engine/_scanner.py`, so a
   violation is an `ImportError`, not a review comment.
3. **Laws live in code.** Anything that must always hold — id uniqueness, scope
   isolation, size caps, path jails — belongs in code, not in a docstring
   asking future contributors to be careful.
4. **No estate-specific values.** Hostnames, project rosters, personal paths and
   emails are configuration, not constants. Default to empty or to a documented
   env var. PRs adding a hardcoded private value will be asked to change it.
5. **Small commits, honest messages.** Say what changed and why; if something is
   still broken after your patch, say that too.

## Tests

`python -m pytest -q` runs the suite serially. With `pytest-xdist` installed,
`python -m pytest -q -n auto` is much faster. Please run the full suite before
opening a PR and mention any test you had to skip, and why.

## Reporting bugs

Include your Python version, your OS, the command you ran, and the full output.
For anything security-related, see [SECURITY.md](SECURITY.md) instead — do not
open a public issue.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0, the same license that covers this project.
