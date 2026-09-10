"""echelon imagine — image CREATION/EDIT via Gemini (the claude-gem paintbrush).

WHY THIS EXISTS (owner, 2026-07-09): the Claude Code harness has no image-generation
tool — a claude-gem worker could SEE (vision through the proxy) but never DRAW. The
creative bridge: a CLI verb any harness reaches through Bash. GeminiProvider already
carries the whole image path (response_modalities, image_config, _extract_image);
this door just drives it and writes the bytes to disk.

  echelon imagine "<prompt>" -o out.png                       # create
  echelon imagine "make the sky dusk" -o v2.png --input v1.png  # edit/reference
  echelon imagine "<prompt>" -o mock.png --aspect 16:9 --size 1K --quality 60

The loop that works (image-first-ui-loop): imagine → Read the PNG back (vision
verifies it) → refine the prompt → imagine again.

Layer: echelon_engine.atoms (a door). Leaf deps only (GeminiProvider).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The Vertex Express image tier proven live 2026-06 (the mockup-render model).
_DEFAULT_MODEL = "gemini-3.1-flash-image"


def _main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        prog="echelon imagine",
        description="Create or edit an image via Gemini and write it to disk.")
    ap.add_argument("prompt", help="what to draw (or the edit instruction when --input is given)")
    ap.add_argument("-o", "--out", required=True, help="output image path (.png)")
    ap.add_argument("--model", default=_DEFAULT_MODEL)
    ap.add_argument("--input", action="append", default=[], metavar="IMG",
                    help="reference/edit input image(s) — repeatable")
    ap.add_argument("--aspect", default=None, help="aspect ratio, e.g. 16:9, 1:1, 9:16")
    # 1K default (not the model's full-res): full-res generation ran ~100-150s inside a
    # claude-gem loop and tripped the harness Bash timeout — 1K lands in ~40s and a worker
    # verifying with vision doesn't need more. Ask for 2K/4K explicitly when it matters.
    ap.add_argument("--size", default="1K", help="image_size: 1K (default), 2K, 4K")
    ap.add_argument("--quality", type=int, default=None,
                    help="output compression quality 1-100 (lower = smaller file)")
    a = ap.parse_args(argv)

    for p in a.input:
        if not Path(p).exists():
            print(f"imagine: input image not found: {p}", file=sys.stderr)
            return 1

    from .providers.gemini import GeminiProvider
    provider = GeminiProvider()
    kwargs: dict = {"response_modalities": ["TEXT", "IMAGE"]}
    if a.aspect:
        kwargs["aspect_ratio"] = a.aspect
    if a.size:
        kwargs["image_size"] = a.size
    if a.quality is not None:
        kwargs["compression"] = a.quality
    if a.input:
        kwargs["images"] = list(a.input)

    resp = provider.send([{"role": "user", "content": a.prompt}],
                         model_id=a.model, **kwargs)
    image_bytes = (resp.raw or {}).get("image_bytes") if resp.raw else None
    if not image_bytes:
        print(f"imagine: no image returned (status={resp.status}, model={a.model})",
              file=sys.stderr)
        if resp.content:
            print(f"  model said: {resp.content[:500]}", file=sys.stderr)
        return 1

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(image_bytes)
    print(f"imagine: wrote {out} ({len(image_bytes)} bytes, model={a.model}, "
          f"tokens in={resp.tokens_in} out={resp.tokens_out})")
    if resp.content:
        print(resp.content.strip())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
