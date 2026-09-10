"""manifest — the FROZEN-CONTEXT contract for swarm dispatches.

THE LAW THIS ENFORCES (owner, 2026-08-19): pointing a swarm at a folder must
never "throw every single thing as context." Context is SELECTED, BUDGETED,
and RECEIPTED:

  SELECTED  — explicit --file paths, or --root NARROWED by --pick globs. A bare
              --root with neither --pick nor --budget is REFUSED with the fix
              printed (the anti-slurp law) unless --all declares the intent.
  BUDGETED  — a byte budget (--budget-kb, default 128) decides how much rides;
              files are ranked (picked-first, then smallest-first so MORE files
              fit) and the cut is reported, never silent.
  RECEIPTED — every dispatch prints the CONTEXT RECEIPT: what rode, what was
              cut, total bytes, and the manifest hash. The receipt is the
              debuggability: "why did the lens miss X?" is answerable.

The manifest hash feeds the frozen/dynamic cache split (context.SwarmContext):
same selection = same bytes = a warm provider cache across seats.
"""
from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

_SOURCE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
                ".json", ".yaml", ".yml", ".toml", ".md", ".sql", ".sh", ".ps1",
                ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp"}
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist",
              "build", ".next", ".turbo", "target", ".tox", ".eggs",
              ".mypy_cache", ".pytest_cache", ".ruff_cache"}

DEFAULT_BUDGET_KB = 128
_PER_FILE_CAP = 32_000


class SlurpRefused(ValueError):
    """Raised when --root arrives with no narrowing and no declared intent."""


@dataclass
class Manifest:
    included: list[tuple[str, int]] = field(default_factory=list)  # (relpath, bytes)
    excluded: list[tuple[str, str]] = field(default_factory=list)  # (relpath, reason)
    total_bytes: int = 0
    budget_bytes: int = 0
    text: str = ""

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:10]

    def receipt(self) -> str:
        lines = [f"CONTEXT RECEIPT  manifest={self.hash}  "
                 f"{len(self.included)} file(s), {self.total_bytes:,}B "
                 f"of {self.budget_bytes:,}B budget"]
        for rel, n in self.included:
            lines.append(f"  + {rel}  ({n:,}B)")
        for rel, why in self.excluded[:12]:
            lines.append(f"  - {rel}  [{why}]")
        if len(self.excluded) > 12:
            lines.append(f"  - ... and {len(self.excluded) - 12} more excluded")
        if not self.included:
            lines.append("  (empty — the dispatch rides on the goal text alone)")
        return "\n".join(lines)


def _scan(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in _SOURCE_EXTS:
            continue
        rel_parts = p.relative_to(root).parts
        if set(rel_parts) & _SKIP_DIRS:
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        out.append(p)
    return out


def build(root: str = "", files: list[str] | None = None,
          picks: list[str] | None = None, budget_kb: int | None = None,
          allow_all: bool = False) -> Manifest:
    """Assemble the frozen file context under the anti-slurp law.

    files  — explicit paths (relative to root when given, else cwd/absolute);
             ALWAYS ride first, in the order given.
    root   — a directory to scan; REQUIRES picks or an explicit budget_kb or
             allow_all=True, otherwise SlurpRefused.
    picks  — glob patterns (fnmatch, against the /-normalized relpath) that
             narrow the root scan.
    """
    picks = [p for p in (picks or []) if p]
    files = list(files or [])
    explicit_budget = budget_kb is not None
    budget = (budget_kb if explicit_budget else DEFAULT_BUDGET_KB) * 1024

    m = Manifest(budget_bytes=budget)
    root_path = Path(root).resolve() if root else Path.cwd()

    candidates: list[tuple[str, Path, bool]] = []  # (relpath, path, was_picked)
    for f in files:
        p = (root_path / f) if root and not Path(f).is_absolute() else Path(f)
        if not p.is_file():
            p = Path(f)
        if p.is_file():
            try:
                rel = str(p.resolve().relative_to(root_path)).replace("\\", "/")
            except ValueError:
                rel = str(p)
            candidates.append((rel, p, True))
        else:
            m.excluded.append((f, "not found"))

    if root:
        if not Path(root).is_dir():
            raise NotADirectoryError(f"--root is not a directory: {root}")
        if not picks and not explicit_budget and not allow_all:
            raise SlurpRefused(
                f"--root {root} with no --pick and no --budget-kb would throw "
                f"the whole tree as context. Narrow it:\n"
                f"  --pick \"*.py\" --pick \"src/**\"     ride only what matches\n"
                f"  --budget-kb 128                    declare the byte budget\n"
                f"  --all                              declare the slurp on purpose"
            )
        scanned = _scan(Path(root).resolve())
        seen = {rel for rel, _, _ in candidates}
        for p in sorted(scanned):
            rel = str(p.relative_to(root_path)).replace("\\", "/")
            if rel in seen:
                continue
            if picks and not any(fnmatch.fnmatch(rel, g) or
                                 fnmatch.fnmatch(Path(rel).name, g)
                                 for g in picks):
                m.excluded.append((rel, "no --pick match"))
                continue
            candidates.append((rel, p, bool(picks)))

    # picked/explicit first (input order), then the rest smallest-first so more fits
    sized = []
    for rel, p, picked in candidates:
        try:
            n = p.stat().st_size
        except OSError:
            m.excluded.append((rel, "unreadable"))
            continue
        sized.append((rel, p, picked, n))
    ordered = ([c for c in sized if c[2]] +
               sorted([c for c in sized if not c[2]], key=lambda c: c[3]))

    blocks: list[str] = []
    for rel, p, _picked, n in ordered:
        if m.total_bytes >= budget:
            m.excluded.append((rel, "over budget"))
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            m.excluded.append((rel, "unreadable"))
            continue
        if len(content) > _PER_FILE_CAP:
            content = content[:_PER_FILE_CAP] + "\n... [truncated at 32KB]"
        blocks.append(f"### {rel}\n```{p.suffix.lstrip('.')}\n{content}\n```")
        m.included.append((rel, len(content)))
        m.total_bytes += len(content)

    m.text = "\n\n".join(blocks)
    return m
