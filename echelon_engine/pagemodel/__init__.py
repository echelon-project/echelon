"""
pagemodel — the ChainBoard ANALYSIS layer (deterministic page-structure extraction + graph + roleflow).

Vocab (kept distinct from the public `chainboard` framework, whose Atom/Chain/Board are enforcement
primitives): SURFACE (a page/board) -> MODULE (a feature container) -> NODE (a component/DOM atom).
A ROLEFLOW is a traversed logic path (event -> fetch -> render), reusing the ChainResult *shape* only.

Pure readers over a target repo. No ECHELON substrate coupling in this first push.
"""
