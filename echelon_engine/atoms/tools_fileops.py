"""tools_fileops.py — the FILE-OPS mixin for ToolRegistry (extracted 2026-06-07).

The read/search/list/map/write/edit/replace methods are the agent's file hands —
cohesive, large (~190 lines), and the second real extraction after _AttachMixin.
Methods moved VERBATIM: they use only self + the registry's attributes
(self._safe, self._read_paths, self._read_cover, self.allow_write, self.root,
self._re) unchanged, so they live here as a mixin ToolRegistry inherits. The
read-before-edit GUARD and the smart head+tail default move with them intact.

Follows the attach-mixin pattern (class-bound code splits as a MIXIN, not free
functions — the estate's settled refactor law). See tools_attach.py, REFACTOR_REPORT.md.
"""
from __future__ import annotations
import json
import re as _re

from echelon_sdk import byte_read, config
from echelon_sdk.exceptions import ToolError   # WELD #4: from sdk, not from .tools (cycle gone)


class _FileOpsMixin:
    """read/search/list/map/write/edit/replace for ToolRegistry (mixin — never instantiated alone)."""

    def _read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        # Byte-mode delegation: if config says byte mode, hand off to byte_read.
        # MUST resolve through _safe FIRST — byte_read opens the raw path, so a relative path
        # would resolve against the process cwd (NOT the sandbox root) and a path outside root
        # would escape the sandbox. _safe both roots the relative path against self.root and
        # enforces the boundary — identical to the line-mode path below. (Bug caught 2026-06-17:
        # a partner dispatched into <estate-root>/AlphaApp couldn't read 'api_app_dash/...' because byte
        # mode skipped this, resolving against ECHELON-AGENT's cwd -> FileNotFoundError.)
        if config.get("file_read.mode", "line") == "byte":
            bp = self._safe(path)
            window = config.get("file_read.window_bytes", 16384)
            peek = config.get("file_read.peek_bytes", 4096)
            # Register the read for the read-before-edit guard — the byte path returned early
            # and skipped registration entirely, so an agent in byte mode could NEVER earn the
            # edit license (the guard fails closed on an unregistered path). Byte windows don't
            # track line coverage: grant the full license only when this call demonstrably
            # returned the entire file (read-from-start with no cap or a cap >= file size);
            # any partial window registers #partial so edit_file still demands a full read.
            size = bp.stat().st_size
            whole = (offset in (None, 0)) and (limit is None or limit >= size) and size <= max(window, peek * 2)
            if whole:
                self._read_paths.add(str(bp)); self._read_paths.discard(str(bp) + "#partial")
            else:
                self._read_paths.add(str(bp) + "#partial")
            return byte_read.read_window(bp, offset, limit, window=window, peek=peek)

        p = self._safe(path)
        if not p.is_file():
            raise ToolError(f"not a file: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        n = len(lines)
        key = str(p)

        if not hasattr(self, "_read_cover"):
            self._read_cover = {}
        covered = self._read_cover.setdefault(key, set())

        def _mark(lo: int, hi: int) -> None:
            covered.update(range(lo, hi + 1))
            if len(covered) >= n:
                self._read_paths.add(key); self._read_paths.discard(key + "#partial")
            else:
                self._read_paths.add(key + "#partial")

        SMALL_LINES, WINDOW, PEEK = 250, 200, 40

        if offset is None and limit is None:
            # SMART DEFAULT (the way a good engineer opens an unfamiliar file): a small file is
            # read whole; a big file shows HEAD + TAIL (the skeleton — imports/top + how it ends)
            # with a gap marker, so the agent orients on SHAPE first, then pages the middle in
            # ~200-LOC windows with offset/limit. A head+tail peek is NOT a full read (the middle
            # is unseen) -> stays #partial, so edit_file still requires reading the relevant window.
            if n <= SMALL_LINES:
                _mark(1, n)
                return f"[whole file, {n} lines]\n{text}"
            head = "".join(lines[:PEEK])
            tail = "".join(lines[-PEEK:])
            _mark(1, PEEK); _mark(n - PEEK + 1, n)
            self._read_paths.discard(key)             # head+tail is not full coverage
            self._read_paths.add(key + "#partial")
            return (f"[{n} lines total — showing HEAD 1-{PEEK} and TAIL {n-PEEK+1}-{n}. "
                    f"Page the middle with read_file(offset=<line>, limit={WINDOW}); reading the "
                    f"window(s) you'll edit satisfies the read-before-edit guard.]\n"
                    f"--- HEAD (1-{PEEK}) ---\n{head}\n"
                    f"...[lines {PEEK+1}-{n-PEEK} not shown — page them]...\n"
                    f"--- TAIL ({n-PEEK+1}-{n}) ---\n{tail}")

        # WINDOWED paged read (default window ~200 lines).
        start = max(1, offset or 1)
        count = limit if limit is not None else WINDOW
        chunk = lines[start - 1:start - 1 + count]
        end = start - 1 + len(chunk)
        _mark(start, end)
        body = "".join(chunk)
        more = "" if key in self._read_paths else \
            f"\n...[paged window; next: read_file(offset={end+1}, limit={WINDOW}) — read the part you'll edit]"
        return f"[lines {start}-{end} of {n}]\n{body}{more}"

    def _search_file(self, path: str, query: str, context: int = 4, regex: bool = True) -> str:
        """grep-and-jump: locate a pattern, return each hit's line number + context. Reading a
        hit's context marks those lines as read (so search -> edit the located spot works)."""
        p = self._safe(path)
        if not p.is_file():
            raise ToolError(f"not a file: {path}")
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        n = len(lines)
        try:
            pat = _re.compile(query if regex else _re.escape(query))
        except _re.error as e:
            raise ToolError(f"bad regex {query!r}: {e}")
        hits = [i + 1 for i, ln in enumerate(lines) if pat.search(ln)]
        if not hits:
            return f"no match for {query!r} in {p.relative_to(self.root)} ({n} lines)"
        if not hasattr(self, "_read_cover"):
            self._read_cover = {}
        covered = self._read_cover.setdefault(str(p), set())
        out = [f"{len(hits)} match(es) for {query!r} in {p.relative_to(self.root)} ({n} lines):"]
        shown = 0
        for h in hits[:25]:
            lo, hi = max(1, h - context), min(n, h + context)
            covered.update(range(lo, hi + 1))
            out.append(f"\n--- line {h} (context {lo}-{hi}) ---")
            for ln in range(lo, hi + 1):
                mark = ">" if ln == h else " "
                out.append(f"{mark}{ln:5} {lines[ln-1]}")
            shown += 1
        # update guard coverage flags
        if len(covered) >= n:
            self._read_paths.add(str(p)); self._read_paths.discard(str(p) + "#partial")
        else:
            self._read_paths.add(str(p) + "#partial")
        if len(hits) > shown:
            out.append(f"\n...[{len(hits)-shown} more matches at lines {hits[shown:shown+15]}]")
        out.append(f"\n(context lines are now read — you can edit_file around any shown hit)")
        return "\n".join(out)

    def _list_files(self, directory: str = ".", pattern: str = "*", recursive: bool = False) -> str:
        d = self._safe(directory)
        if not d.is_dir():
            raise ToolError(f"not a directory: {directory}")
        it = d.rglob(pattern) if recursive else d.glob(pattern)
        names = sorted(str(p.relative_to(self.root)) for p in it if p.is_file())
        return json.dumps(names[:500]) + (f"\n...[{len(names)} total]" if len(names) > 500 else "")

    def _map_repo(self, directory: str = ".", signatures: bool = True) -> str:
        """Extract a repo's STRUCTURE (imports/classes/function signatures) so the agent reads the
        architecture instead of cat-ing files. Reclaim of AIFACTOR graph_builder, on stdlib ast (no
        libcst/networkx dep). Confined to the sandbox via _safe; bounded output so a big repo gives
        the spine, not an unbounded dump. See repo_graph.py, memory summariser-is-symptom-of-brute-reading."""
        d = self._safe(directory)
        if not d.is_dir():
            raise ToolError(f"not a directory: {directory}")
        from .repo_graph import build_graph, render_outline
        graph = build_graph(d)
        if graph["stats"]["files"] == 0:
            return (f"no Python files under {directory} — map_repo reads .py structure. "
                    "Use list_files for a non-Python tree, or point at a subtree that has code.")
        return render_outline(graph, signatures=signatures)

    def _write_file(self, path: str, content: str) -> str:
        if not self.allow_write:
            raise ToolError("writes are disabled for this run")
        p = self._safe(path, for_write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._read_paths.add(str(p))   # the agent authored this content; it's "read" now
        return f"wrote {len(content)} chars to {p.relative_to(self.root)}"

    def _edit_file(self, path: str, old_string: str, new_string: str,
                   replace_all: bool = False) -> str:
        """Surgical edit — the agent's proper edit protocol (owner: stop pity-rewriting whole
        files). Replace exact old_string with new_string; old_string must be unique unless
        replace_all. Mirrors Claude Code's Edit so the model already knows the discipline."""
        if not self.allow_write:
            raise ToolError("writes are disabled for this run")
        p = self._safe(path, for_write=True)
        if not p.is_file():
            raise ToolError(f"not a file (use write_file to create): {path}")
        # GUARD — MUST READ BEFORE EDIT (like Claude Code). An edit on a file you haven't read
        # this session is a blind edit on stale assumptions. Read it first.
        if str(p) not in self._read_paths:
            hint = " (you read it only partially — read it in FULL first)" \
                if (str(p) + "#partial") in self._read_paths else ""
            raise ToolError(f"read_file '{path}' before editing it{hint} — never edit a file you "
                            f"haven't read this session.")
        if old_string == new_string:
            raise ToolError("old_string and new_string are identical — nothing to change")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old_string)
        if n == 0:
            raise ToolError("old_string not found — it must match the file EXACTLY (whitespace, "
                            "indentation included). Read the file and copy the exact text.")
        if n > 1 and not replace_all:
            raise ToolError(f"old_string appears {n} times — not unique. Add surrounding context "
                            f"to make it unique, or pass replace_all=true.")
        new_text = text.replace(old_string, new_string)
        p.write_text(new_text, encoding="utf-8")
        self._read_paths.add(str(p))   # the agent has just seen the change; keep edit license valid
        where = f"{n} occurrence(s)" if replace_all else "1 occurrence"
        return (f"edited {p.relative_to(self.root)} ({where}; "
                f"{len(text)}->{len(new_text)} chars) — surgical, rest untouched")

    def _replace_in_file(self, path: str, pattern: str, replacement: str,
                         regex: bool = True, first: bool = False) -> str:
        """Search-and-replace (string or regex). For pattern-shaped changes across a file."""
        if not self.allow_write:
            raise ToolError("writes are disabled for this run")
        p = self._safe(path, for_write=True)
        if not p.is_file():
            raise ToolError(f"not a file (use write_file to create): {path}")
        if str(p) not in self._read_paths:
            raise ToolError(f"read_file '{path}' before replacing in it — never change a file you "
                            "haven't read this session.")
        text = p.read_text(encoding="utf-8", errors="replace")
        count = 1 if first else 0   # re.sub count: 0 = all
        try:
            pat = _re.compile(pattern if regex else _re.escape(pattern))
        except _re.error as e:
            raise ToolError(f"bad regex {pattern!r}: {e}")
        n_found = len(pat.findall(text))
        if n_found == 0:
            raise ToolError(f"pattern {pattern!r} not found — nothing to replace.")
        repl = replacement if regex else replacement.replace("\\", "\\\\")
        new_text, n_done = pat.subn(repl, text, count=count)
        if new_text == text:
            raise ToolError("replacement produced no change (pattern matches but replacement is identical)")
        p.write_text(new_text, encoding="utf-8")
        self._read_paths.add(str(p))
        scope = "first match" if first else f"all {n_done} match(es)"
        return (f"replaced {scope} of {pattern!r} in {p.relative_to(self.root)} "
                f"({len(text)}->{len(new_text)} chars)")
