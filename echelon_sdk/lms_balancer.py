#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
lms_balancer — VRAM-aware load balancing for a LOCAL LM-Studio box, driven through
the `lms` CLI (the HTTP :19999 server has NO load/unload endpoint; the CLI does).

Owner's rule (2026-06-09):
  - EVICT BEFORE LOAD so a model is never squeezed into too little VRAM (which would
    spill to RAM and degrade it).
  - BUT if the GPU has free slots, co-loading >1 model is fine — pack up to an 80%
    VRAM cap to stay safe.

So this packs a set of models into BATCHES that each fit under the cap (greedy,
first-fit-decreasing), loading a batch's models together and unloading the prior
batch first. Footprints come from `lms load --estimate-only -y` (calculates without
loading). VRAM total is read once.

CRITICAL CLI GOTCHA: `lms load <key>` drops into an INTERACTIVE picker when the key
matches >1 model — it HANGS in a non-TTY. ALWAYS pass `-y` (auto-approve, takes the
first match). Every call here uses -y.
"""
import os
import re
import subprocess
from pathlib import Path


def _lms_bin() -> str:
    """The LM Studio CLI. ECHELON_LMS_BIN wins; else the per-user default install
    path for this platform; else bare `lms` and let PATH resolve it."""
    env = os.environ.get("ECHELON_LMS_BIN")
    if env:
        return env
    default = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    return str(default) if default.exists() else "lms"


LMS = _lms_bin()
# Pack cap as a fraction of TOTAL VRAM. The footprints come from `--estimate-only`
# which EXCLUDES runtime KV-cache / parallel / context allocation — measured ~1.4x
# inflation (a 10.8 GiB estimate-pack hit 15.45/16 GiB = 97% at runtime, near OOM).
# So pack to 0.60 of estimate (not 0.80) to leave room for that inflation, AND load
# judges lean (parallel 1 + small context) since warmth prompts are short. Root cause
# of the "spill": over-packing on under-counted estimates. See memory
# local-lm-studio-control-and-vram-balancer.
VRAM_CAP_FRACTION = 0.60
LOAD_CONTEXT = 4096               # warmth prompts are short; shrink KV-cache
LOAD_PARALLEL = 1                 # no concurrency needed; parallel 2 doubled KV-cache
_TOTAL_RE = re.compile(r"Estimated Total Memory:\s*([\d.]+)\s*GiB", re.I)


def _run(args, timeout=180):
    """Run an lms subcommand with -y already appended by the caller. Returns stdout."""
    try:
        r = subprocess.run([LMS, *args], capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return f"__ERR__ {e}"


def vram_total_gib():
    """Total GPU VRAM in GiB, via the Windows registry qwMemorySize (Win32 AdapterRAM
    is capped/wrong for >4GB cards). Falls back to 16.0 if it can't read it."""
    ps = (
        r"$b='HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}';"
        r"Get-ChildItem $b -EA SilentlyContinue|%{"
        r"$m=(Get-ItemProperty $_.PSPath -Name 'HardwareInformation.qwMemorySize' -EA SilentlyContinue)."
        r"'HardwareInformation.qwMemorySize'; if($m){[math]::Round($m/1GB,2)}}"
    )
    out = _run_ps(ps)
    vals = [float(x) for x in re.findall(r"[\d.]+", out)]
    return max(vals) if vals else 16.0


def _run_ps(script):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           capture_output=True, text=True, timeout=30)
        return (r.stdout or "")
    except Exception:
        return ""


def estimate_gib(model_key):
    """VRAM a model needs, via --estimate-only (does NOT load). -y suppresses the
    interactive picker. Returns float GiB, or None if it couldn't estimate."""
    out = _run(["load", model_key, "--estimate-only", "-y"], timeout=120)
    m = _TOTAL_RE.search(out)
    return float(m.group(1)) if m else None


def loaded_keys():
    """Currently-resident model identifiers, from `lms ps --json` (STRUCTURED — the
    plain `lms ps` text printed help lines like '    lms load <model path>' on the
    empty path, which the old line-parser mistook for a loaded model named 'lms' →
    never-empty → wait_until_clear() spun the full timeout every call. JSON is the
    source of truth: [] when empty)."""
    out = _run(["ps", "--json"], timeout=30)
    import json as _json
    m = re.search(r"\[.*\]", out, re.DOTALL)
    if not m:
        return []
    try:
        arr = _json.loads(m.group(0))
    except Exception:
        return []
    keys = []
    for e in arr:
        if isinstance(e, dict):
            k = e.get("identifier") or e.get("modelKey") or e.get("path") or e.get("model")
            if k:
                keys.append(str(k))
    return keys


def wait_until_clear(timeout_s=60, settle_s=1.5):
    """Block until NO models are resident, then a short settle so the driver actually
    reclaims VRAM. THE FIX for the spill race: `lms unload` returns before the GPU has
    freed memory; loading immediately races it and spills. Poll ps -> empty, then settle."""
    import time
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not loaded_keys():
            time.sleep(settle_s)            # let the driver finish freeing VRAM
            return True
        time.sleep(0.8)
    return False                            # timed out still-loaded (caller proceeds anyway)


def load(model_key, ttl=900):
    """Explicitly load (idempotent), forcing FULL GPU offload + a lean footprint:
    --gpu max  (every layer to VRAM — we packed to fit, don't let auto push to CPU),
    --parallel 1 + --context-length LOAD_CONTEXT  (shrink the KV-cache that the
    --estimate-only footprint ignored and that pushed real VRAM to 97%). -y = no picker."""
    return _run([
        "load", model_key, "-y", "--ttl", str(ttl),
        "--gpu", "max", "--parallel", str(LOAD_PARALLEL),
        "-c", str(LOAD_CONTEXT),
    ], timeout=300)


def unload(model_key):
    # single-model evict: do NOT wait-for-clear (other batch members may stay resident).
    return _run(["unload", model_key], timeout=60)


def wait_until_loaded(model_key, timeout_s=300, settle_s=1.0):
    """Block until `model_key` shows resident in `lms ps` (handles the async load), then
    a short settle. Returns True if it appeared. Match is prefix-tolerant (ps may print a
    truncated/identifier form)."""
    import time
    deadline = time.time() + timeout_s
    key0 = model_key.split("-")[0]
    while time.time() < deadline:
        ks = loaded_keys()
        if any(k == model_key or k.startswith(model_key[:30]) or model_key.startswith(k) or k.startswith(key0) for k in ks):
            time.sleep(settle_s)
            return True
        time.sleep(1.0)
    return False


def unload_all():
    """Evict everything AND WAIT for the GPU to actually be clear (not just for the
    command to return). Without the wait, the next load() spills (owner-reported)."""
    out = _run(["unload", "--all"], timeout=60)
    wait_until_clear()
    return out


def plan_batches(models, cap_gib=None, vram_gib=None):
    """Greedy first-fit-decreasing pack of `models` into batches each <= cap.
    Returns (batches, footprints, cap_gib). A model bigger than the cap gets its
    own solo batch (the estimator decides if it loads at all)."""
    if vram_gib is None:
        vram_gib = vram_total_gib()
    if cap_gib is None:
        cap_gib = round(vram_gib * VRAM_CAP_FRACTION, 2)
    foot = {}
    for m in models:
        foot[m] = estimate_gib(m) or 9999.0   # unknown -> treat as huge (solo)
    order = sorted(models, key=lambda m: foot[m], reverse=True)
    batches = []
    for m in order:
        placed = False
        for batch in batches:
            if sum(foot[x] for x in batch) + foot[m] <= cap_gib:
                batch.append(m)
                placed = True
                break
        if not placed:
            batches.append([m])
    return batches, foot, cap_gib


if __name__ == "__main__":
    import sys
    models = sys.argv[1:] or [
        "qwen3.5-9b-claude-4.6-os-auto-variable-heretic-uncensored-thinking-max-neocode-imatrix",
        "gemma-4-e4b-uncensored-hauhaucs-aggressive",
        "qwen2.5-14b-instruct",
        "qwen3-4b-instruct-2507-polaris-alpha-distill-heretic-abliterated-i1",
        "llama-3.2-3b-instruct-abliterated",
    ]
    v = vram_total_gib()
    batches, foot, cap = plan_batches(models, vram_gib=v)
    print(f"VRAM {v} GiB, cap {cap} GiB ({int(VRAM_CAP_FRACTION*100)}%)\n")
    for f in models:
        print(f"  {foot[f]:5.2f} GiB  {f[:55]}")
    print(f"\n{len(batches)} batch(es):")
    for i, b in enumerate(batches):
        print(f"  batch {i+1} ({sum(foot[x] for x in b):.2f} GiB): {', '.join(x.split('-')[0] for x in b)}")
