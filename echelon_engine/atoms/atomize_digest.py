"""T2 of the /echelon-init atomization pipeline (T2 gemma-digest -> T1 Sonnet-reason -> T3 write).

The wallet law (owner 2026-06-16): do NOT dispatch a Sonnet subagent per file. Instead the cheap
LOCAL floor (gemma-4-e4b, 131k window) reads each raw molecule and COMPACTS it into a structured
lesson-map. Sonnet (T1) then reasons over the COMPACT digests only — never the raw bulk — which is
what makes atomizing a 160-file estate affordable. This script is T2: raw molecule -> compact JSON
lesson-map per file, written to memory/_atoms/_digests/<name>.json for T1 to reason over.

Run:  python -X utf8 -m echelon_engine.atoms.atomize_digest --mem "<memory dir>" [--only f.md] [--limit N]
(Promoted from eval/ into the real package 2026-06-17 — /echelon-init Phase 1B runs it as production.)
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from echelon_sdk.keys import load_lmstudio_key
from echelon_sdk.config import floor_endpoint

LMS = floor_endpoint("/v1/chat/completions")  # config 'floor.host' / LM_ENDPOINT env, not a hardcoded host
MODEL = "google/gemma-4-e4b"
_TOKEN: str | None = None  # lazy — a module import must never require a configured key


def _token() -> str:
    global _TOKEN
    if _TOKEN is None:
        _TOKEN = load_lmstudio_key()
    return _TOKEN

DIGEST_PROMPT = """You read ONE dense memory file and COMPACT it into a map of the distinct LESSONS it holds.
A "lesson" = one indivisible takeaway: a single trap, one topology fact, one design law, one decision,
one recipe step-group. A dense file fuses several; your job is to LIST them, not merge them.

ABSOLUTE RULES (truthfulness is everything):
1. NEVER invent a fact, number, name, path, or date not in the source. Copy facts EXACTLY.
2. NEVER drop a load-bearing fact. Every trap, number, command, and decision must land in some lesson.
3. Do NOT split mere emphasis: a **bold lead-in** inside one explanation is NOT its own lesson.
4. If the file genuinely holds ONE lesson, return exactly one entry (do not force a split).

For EACH lesson output: a short kebab "slug", a one-line "gist", the exact load-bearing
"facts" (numbers/names/paths/commands/dates), and any cross-references it mentions as "links".

JSON FORMAT RULES (a malformed array breaks the pipeline — obey exactly):
- "facts" is a FLAT array of plain strings. NO nested arrays, NO "[" or "]" characters INSIDE a
  fact string, NO parentheses-then-bracket. If a fact contains a list, write it as ONE string with
  commas: "covers 0009, 0024, 0027" — never "0009", ["0024"].
- "links" is a FLAT array of bare slug strings — strip the [[ ]] brackets: write "some-slug", not
  "[[some-slug]]".
- Keep each fact SHORT (one clause). Do not paste long quoted blocks.

Output ONLY a JSON object on ONE pass: {"lessons": [{"slug": "...", "gist": "...", "facts": ["..."], "links": ["..."]}]}.
No prose, no markdown fence, no trailing commentary.

FILE NAME: %s
SOURCE:
---
%s
---
Output the JSON object now."""


def call(prompt, timeout=300):
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0.0, "max_tokens": 8000}).encode()
    req = urllib.request.Request(LMS, data=body, headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {_token()}"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"], round(time.time() - t0, 1)


def parse(raw):
    """Tolerant: gemma reliably emits a clean lessons ARRAY but sometimes omits the wrapper's final
    `}` (verified: brace count off by one, array `]` present). So parse the ARRAY directly — find the
    first `[` after "lessons" through its matching `]` — rather than requiring a perfectly-closed
    object. Falls back to whole-object parse for clean output."""
    txt = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    txt = re.sub(r"```(json)?", "", txt)
    # 1) clean object path
    m = re.search(r"\{.*\}", txt, re.DOTALL)
    if m:
        try:
            o = json.loads(m.group(0))
            if isinstance(o, dict) and "lessons" in o:
                return o
        except Exception:
            pass
    # 2) extract the lessons array by brace-matching from the first '[' after "lessons" — handles the
    #    cheap+common "gemma omitted the wrapper's final }" case. Deeper malformations (a stray nested
    #    bracket inside facts) are NOT regex-patched here — the call site RE-ASKS gemma to fix its own
    #    JSON (owner: make the tool rewrite it right, by asking the model — not brittle regex surgery).
    key = txt.find('"lessons"')
    start = txt.find("[", key if key >= 0 else 0)
    if start < 0:
        return None
    depth, end = 0, -1
    for i in range(start, len(txt)):
        c = txt[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        lessons = json.loads(txt[start:end])
        return {"lessons": lessons} if isinstance(lessons, list) and lessons else None
    except Exception:
        return None


REPAIR_PROMPT = """The JSON below is malformed (a Python json.loads failed). Fix it to STRICTLY VALID JSON.
Keep ALL the content — every lesson, fact, and link — change ONLY the syntax. Rules: "facts" and "links"
are FLAT arrays of plain strings; NO nested arrays; NO "[" or "]" inside a string; strip [[ ]] from links.
Output ONLY the corrected JSON object {"lessons":[...]}, no prose, no fence.

BROKEN JSON:
%s

Corrected JSON:"""


def digest_one(prompt, max_repairs=2):
    """Call gemma for a digest; if the JSON is malformed, RE-ASK gemma to fix its own output (the
    tool owns correctness, the model owns content — owner 2026-06-16). Returns (obj_or_None, secs, raw)."""
    raw, secs = call(prompt)
    o = parse(raw)
    total = secs
    tries = 0
    while o is None and tries < max_repairs:
        tries += 1
        # send the broken text back and ask for valid JSON only
        bad = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)[:12000]
        raw, s = call(REPAIR_PROMPT % bad, timeout=240)
        total += s
        o = parse(raw)
    return o, round(total, 1), raw


def lms(args, timeout=300):
    return subprocess.run(["lms", *args], capture_output=True, text=True, timeout=timeout)


def main(argv=None):
    # The dispatcher calls every verb as fn(rest) (__main__.py:445). A zero-arg main() made this
    # verb uninvokable — `atomize-digest --help` raised TypeError before argparse ever ran.
    ap = argparse.ArgumentParser(prog="echelon atomize-digest")
    ap.add_argument("--mem", required=True)
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)  # gemma serves up to 4 parallel (owner, 2026-06-16)
    a = ap.parse_args(argv)

    out_dir = os.path.join(a.mem, "_atoms", "_digests")
    os.makedirs(out_dir, exist_ok=True)
    files = [f for f in os.listdir(a.mem) if f.endswith(".md") and f != "MEMORY.md"]
    if a.only:
        want = {x.strip() for x in a.only.split(",")}
        files = [f for f in files if f in want]
    files.sort()
    if a.limit:
        files = files[:a.limit]

    todo = [f for f in files if not os.path.exists(os.path.join(out_dir, f.replace(".md", ".json")))]
    cached = len(files) - len(todo)
    print(f"T2 DIGEST: {len(files)} files ({cached} cached) on {a.workers} workers -> {out_dir}", flush=True)
    lms(["unload", "--all"], timeout=120)
    lms(["load", MODEL, "-y", "--ttl", "3600"], timeout=300)

    def work(fn):
        dst = os.path.join(out_dir, fn.replace(".md", ".json"))
        src = open(os.path.join(a.mem, fn), encoding="utf-8").read()
        o, secs, raw = digest_one(DIGEST_PROMPT % (fn, src))   # includes the re-ask repair loop
        if not o:
            open(dst + ".raw", "w", encoding="utf-8").write(raw)
            return fn, None, secs
        o["_source"] = fn
        json.dump(o, open(dst, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        return fn, len(o["lessons"]), secs

    ok = fail = 0
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, fn): fn for fn in todo}
        for fut in as_completed(futs):
            done += 1
            fn = futs[fut]
            try:
                fn, n, secs = fut.result()
                if n is None:
                    print(f"  [{done}/{len(todo)}] {fn}  PARSE-FAIL ({secs}s)", flush=True); fail += 1
                else:
                    print(f"  [{done}/{len(todo)}] {fn}  {n} lessons ({secs}s)", flush=True); ok += 1
            except Exception as e:
                print(f"  [{done}/{len(todo)}] {fn}  ERROR {str(e)[:120]}", flush=True); fail += 1
    lms(["unload", "--all"], timeout=120)
    print(f"\nDONE: {ok} digested (+{cached} cached), {fail} failed -> {out_dir}", flush=True)


if __name__ == "__main__":
    main()
