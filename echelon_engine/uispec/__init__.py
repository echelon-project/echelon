"""
uispec — the ChainBoard REWORK/DESIGN layer (gemini-driven generate→gate pipelines).

Sibling to `pagemodel` (the deterministic ANALYSIS layer). Where pagemodel reads a page
and emits a structural GRAPH with no LLM, `uispec` runs the *judgment* pipelines the page-rework
arc pioneered as throwaway scripts: it feeds real page code to a PRO vision model to produce
component SPECs, forensic AUDITs, BLUEPRINT contracts, envelope migrations, and kit components —
each paired with a SKEPTIC GATE run by a *different* model that refutes the artifact against the
real code (a clearing must come from a context that did not build it).

Every stage reduces to the same skeleton (see `_runner.py`):
    resolve page code  ->  build a task prompt  ->  GeminiProvider.send()  ->  strip/validate  ->  write.
The stages differ only in their TASK prompt + I/O contract; those live in `stages.py`.

Vocab shared with pagemodel: SURFACE (page) -> MODULE (feature container) -> NODE (component).
This layer consumes pagemodel's catalogue/digest as optional input and emits the human-gated
rework docs the builder then implements.
"""
