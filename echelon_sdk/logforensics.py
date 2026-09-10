"""logforensics — scan Claude Code session logs for pathological assistant turns.

Born from a real hunt (2026-06-07): the owner remembered a Sonnet session that
finished a task, then kept narrating about the action, and finally streamed
newlines-with-words non-stop until the stream was cut. Searching for it produced
six one-off scanners; this module is their consolidation — a reusable organ ECHELON
can point at any ~/.claude/projects store (or the in-repo harvested copies) to find
the "task-done-blindness" failure class and its cousins.

THE FAILURE CLASSES IT DETECTS (per assistant turn):
  - newline_run   : a run of >=N blank newlines (whitespace collapse tail)
  - tail_ws       : trailing whitespace/newline block at the very end (cut-stream shape)
  - line_repeat   : the SAME short line emitted many times consecutively (word-line spam,
                    e.g. lines.append("") x11 — a model drifting into repetitive emission)
  - line_freq     : one line value repeated MANY times total (not necessarily consecutive)
  - by_loc        : abnormally high LINE COUNT (newline-per-word floods rank here)
  - max_tokens    : stop_reason == 'max_tokens' (a generation that hit the ceiling)
  - aborted       : stop_reason is None on a large turn (streaming/partial — a cut stream
                    leaves NO clean stop_reason; this is the fingerprint of an interrupt)

WHY THIS MATTERS TO ECHELON: this is the origin wound behind cold-termination + the
convergence trigger. A loop that finishes work but has no clean terminal over-runs into
narration and, at the limit, newline-flood. This tool makes that pattern findable in any
log corpus — the eye that catches the disease the architecture is built to cure.
See: compiler-era-fork-field, warmth-convergence-is-the-synthesis-trigger,
drift-guard-is-reasoning-not-steps.

Pure stdlib. No deps. Reusable as a module (scan_corpus / scan_file) or a CLI:
    python -m echelon_sdk.logforensics <root> [--min N] [--top K] [--loc] [--json]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field, asdict

_NL_RUN = re.compile(r"\n{2,}")


@dataclass
class TurnSignature:
    """The pathology fingerprint of a single assistant turn."""
    file: str
    line: int
    project: str = ""
    stop_reason: str | None = None
    model: str = ""
    text_len: int = 0
    n_lines: int = 0
    newline_run: int = 0          # longest run of consecutive blank newlines
    tail_ws: int = 0              # trailing whitespace chars
    line_repeat: int = 0          # longest run of identical consecutive short lines (WITH words)
    line_freq: int = 0            # max total count of any single non-empty line
    line_freq_value: str = ""     # the line that repeated most
    flags: list[str] = field(default_factory=list)
    tail: str = ""                # last ~140 chars (for eyeballing)

    @property
    def severity(self) -> int:
        """A single sortable score: the worst of the repetition/flood signals."""
        return max(self.newline_run, self.tail_ws, self.line_repeat, self.line_freq)


def _assistant_text(obj: dict) -> tuple[str | None, str | None, str]:
    """Extract (joined text, stop_reason, model) from a log record IFF it's an assistant turn."""
    msg = obj.get("message", obj)
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None, None, ""
    content = msg.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
    elif isinstance(content, str):
        parts.append(content)
    return "".join(parts), msg.get("stop_reason"), msg.get("model") or ""


def analyze_text(txt: str) -> dict:
    """Compute the raw pathology metrics for a turn's text. Pure; no I/O."""
    lines = txt.split("\n")
    nonempty = [l.strip() for l in lines if l.strip()]

    longest_nl = max((len(m) for m in _NL_RUN.findall(txt)), default=0)
    tail_ws = len(txt) - len(txt.rstrip("\n \t\r"))

    # consecutive identical SHORT lines that CONTAIN words (the word-line spam signature).
    line_repeat = 0
    run = 1
    for a, b in zip(lines, lines[1:]):
        if a == b and a.strip():            # identical AND non-empty (has content)
            run += 1
            line_repeat = max(line_repeat, run)
        else:
            run = 1

    mc = Counter(nonempty).most_common(1)
    line_freq, line_freq_value = (mc[0][1], mc[0][0]) if mc else (0, "")

    return {
        "n_lines": txt.count("\n") + 1,
        "newline_run": longest_nl,
        "tail_ws": tail_ws,
        "line_repeat": line_repeat,
        "line_freq": line_freq,
        "line_freq_value": line_freq_value[:80],
    }


# repeated structural tokens that are NORMAL in good output (code/markdown), so we don't
# flag a model writing a real source file or a long report as "pathological".
_BENIGN_REPEATS = {"```", "```python", "```js", "```ts", "---", "}", ")", "{", "(", ">",
                   "</div>", "|", "-", "*", "#", ""}


def classify(sig: TurnSignature, min_metric: int) -> list[str]:
    """Decide which failure-class flags apply. Benign structural repetition is excluded."""
    flags: list[str] = []
    if sig.stop_reason == "max_tokens":
        flags.append("max_tokens")
    if sig.newline_run >= min_metric:
        flags.append("newline_run")
    if sig.tail_ws >= min_metric:
        flags.append("tail_ws")
    # word-line spam: repeated line must carry actual words (not a benign structural token)
    val = sig.line_freq_value.strip()
    has_words = len(val) > 3 and not val.startswith(tuple(_BENIGN_REPEATS)) and val not in _BENIGN_REPEATS
    if sig.line_repeat >= max(5, min_metric) and has_words:
        flags.append("line_repeat")
    if sig.line_freq >= 10 and has_words:
        flags.append("line_freq")
    # a big turn with NO stop_reason = streaming/partial = the cut-stream fingerprint
    if sig.stop_reason is None and sig.text_len > 4000:
        flags.append("aborted")
    return flags


def scan_file(path: str, project: str = "", min_metric: int = 5) -> list[TurnSignature]:
    """Scan one .jsonl session log; return flagged assistant turns."""
    out: list[TurnSignature] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                txt, sr, model = _assistant_text(obj)
                if not txt:
                    continue
                m = analyze_text(txt)
                sig = TurnSignature(
                    file=os.path.basename(path), line=i, project=project,
                    stop_reason=sr, model=model, text_len=len(txt),
                    n_lines=m["n_lines"], newline_run=m["newline_run"],
                    tail_ws=m["tail_ws"], line_repeat=m["line_repeat"],
                    line_freq=m["line_freq"], line_freq_value=m["line_freq_value"],
                    tail=repr(txt[-140:]),
                )
                sig.flags = classify(sig, min_metric)
                if sig.flags:
                    out.append(sig)
    except OSError:
        pass
    return out


def scan_corpus(root: str, min_metric: int = 5) -> list[TurnSignature]:
    """Recursively scan every .jsonl under root. Project = first path segment under root."""
    hits: list[TurnSignature] = []
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(f, root)
        project = rel.split(os.sep)[0]
        hits.extend(scan_file(f, project=project, min_metric=min_metric))
    hits.sort(key=lambda s: s.severity, reverse=True)
    return hits


def loc_ranking(root: str, top: int = 20) -> list[TurnSignature]:
    """All assistant turns ranked by line count (newline-per-word floods surface here even
    when no other signature trips). Does not filter — pure LOC view."""
    rows: list[TurnSignature] = []
    for f in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
        rel = os.path.relpath(f, root)
        project = rel.split(os.sep)[0]
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    txt, sr, model = _assistant_text(obj)
                    if not txt:
                        continue
                    rows.append(TurnSignature(
                        file=os.path.basename(f), line=i, project=project,
                        stop_reason=sr, model=model, text_len=len(txt),
                        n_lines=txt.count("\n") + 1, tail=repr(txt[-100:])))
        except OSError:
            pass
    rows.sort(key=lambda s: s.n_lines, reverse=True)
    return rows[:top]


def _main() -> None:
    ap = argparse.ArgumentParser(
        description="Scan Claude Code session logs for pathological assistant turns "
                    "(task-done-blindness: newline-flood, word-line spam, runaway, aborted).")
    ap.add_argument("root", help="A directory of .jsonl logs (e.g. ~/.claude/projects)")
    ap.add_argument("--min", type=int, default=5, help="Minimum metric to flag (default 5)")
    ap.add_argument("--top", type=int, default=25, help="Show top N hits (default 25)")
    ap.add_argument("--loc", action="store_true", help="Also show the by-line-count ranking")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    root = os.path.expanduser(args.root)
    hits = scan_corpus(root, min_metric=args.min)

    if args.json:
        print(json.dumps([asdict(h) for h in hits[:args.top]], indent=2))
        return

    print(f"scanned corpus: {root}")
    print(f"=== {len(hits)} flagged assistant turns (showing top {args.top}) ===\n")
    for h in hits[:args.top]:
        print(f"sev={h.severity:>5} {','.join(h.flags):<32} stop={h.stop_reason} "
              f"len={h.text_len} lines={h.n_lines} [{h.project}] {h.file} L{h.line}")
        if h.line_freq_value:
            print(f"   repeated-line: {h.line_freq_value!r}  (x{h.line_freq})")
        print(f"   tail: {h.tail}\n")

    if args.loc:
        print("=" * 60 + "\n  LOC RANKING (assistant turns by line count)\n" + "=" * 60)
        for h in loc_ranking(root, top=args.top):
            avg = h.text_len / h.n_lines if h.n_lines else 0
            print(f"LINES={h.n_lines:>5} chars={h.text_len:>7} avg/line={avg:5.1f} "
                  f"stop={h.stop_reason} [{h.project}] {h.file} L{h.line}")


if __name__ == "__main__":
    _main()
