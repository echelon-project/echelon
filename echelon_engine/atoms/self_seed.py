"""self_seed — bootstrap a FRESH bank with ECHELON's own self-knowledge.

THE GAP (owner 2026-06-22): a brand-new bank (a fresh ECHELON_HOME) is EMPTY, so
the first `echelon_recall("what is echelon")` returns COLD — the substrate can't
explain itself, and a tool-using model gets nothing to lean on. Fix: on first init
of an empty bank, plant a small set of self-describing ATOMS (what echelon is, the
protocol, the tools, how to add memory, cartridges) + a CARD stitching the
how-it-works arc. Then the very first recall is WARM.

DESIGN (owner's calls): seed content is INLINE here (canon-in-code, no extra
files); planted into scope `echelon`; ONLY when the bank is empty (never re-seed a
working estate — checked via the scope having zero atoms). Idempotent anyway
(content-addressed), but the empty-check keeps a live estate untouched.

The atoms are kept in sync IN SPIRIT with echelon_engine/atoms/gate.py (the gate
canon); they are the tool-flavored, recall-able form of that doctrine.
"""
from __future__ import annotations


# Each: (slug, description, kind, body). description = the one-line warmth hook
# recall ranks on; body = the single lesson, self-contained. [[links]] become
# atlas edges. Written tool-flavored (an MCP model reads these), not CLI-flavored.
_SEED_ATOMS: list[tuple[str, str, str, str]] = [
    ("echelon-what-it-is",
     "WHAT IS ECHELON / DEFINE ECHELON / what is echelon / who am I — the definition and overview, the answer to the bare question 'what is echelon'. ECHELON is a persistent memory + reasoning SUBSTRATE you reach through the echelon_* tools; it is YOUR earned memory, not a generic database. In four claims: memory is a weight-adjustor, recall is foveated, significance is earned by use, the core is a swappable cartridge.",
     "reference",
     "ECHELON is a memory + reasoning SUBSTRATE — a bank of earned knowledge you reach "
     "through the echelon_* tools. It is not a wiki or a generic database; it is YOUR "
     "memory, and it re-shapes how you reason when you recall from it.\n\n"
     "The four claims it runs on (each settled and seen to work):\n"
     "1. A memory is a WEIGHT-ADJUSTOR, not a record — reading an atom RE-SHAPES your "
     "thinking, it doesn't just inform you. So one lesson per atom.\n"
     "2. Recall is FOVEATED VISION — query by INTENT and the atoms that matter come into "
     "focus, ranked by warmth. WARM = known ground that paid off before; COLD = new ground.\n"
     "3. Significance is EARNED BY USE, never asserted — an atom is born neutral and gains "
     "weight only when it's actually reused successfully. Warm = it truly worked before.\n"
     "4. The core is a CARTRIDGE — an earned capability you PLUG IN to BE it.\n\n"
     "Use it through the door: call echelon_recall with your intent. See "
     "[[echelon-reflex-vs-think]], [[echelon-recall-is-the-door]], [[echelon-the-tools]]."),

    ("echelon-recall-is-the-door",
     "The way to USE echelon is echelon_recall(intent) — state in one sentence what you're about to do; the warmest earned atoms surface with a warm/cold verdict. You don't browse the bank; you query an intent and let foveation surface the 2-3 atoms that matter.",
     "reference",
     "You do NOT browse echelon or page in a catalog. You USE it by calling echelon_recall "
     "with an INTENT — one sentence of what you're about to do or answer. The bank foveates: "
     "the 2-3 atoms matching that intent surface sharp, ranked by warmth, the rest stay "
     "dormant-but-reachable.\n\n"
     "Read the VERDICT as a router (it tells you how to proceed):\n"
     "- WARM: known ground — the surfaced atoms are your earned experience; lean on them.\n"
     "- COLD: new ground — nothing earned here yet; think fresh, then offer to remember it.\n\n"
     "To take up a FULL atom body after a truncated preview, call echelon_remember(slug) — "
     "that read is witnessed and earns. A topic you expect but don't see is DORMANT, not "
     "gone — re-query with a sharper intent. See [[echelon-reflex-vs-think]], "
     "[[echelon-what-it-is]]."),

    ("echelon-reflex-vs-think",
     "The ONE routing law: recall FIRST, then read the verdict. WARM → REFLEX (re-tread the proven path, trust the atom over your training). COLD → THINK (generate a fresh move, then crystallize what worked back as a new atom). Confidence IS the routing signal.",
     "feedback",
     "REFLEX vs THINK is the single law that governs every use of echelon.\n\n"
     "Before answering anything about echelon, your own memory, the estate, or past work, "
     "call echelon_recall FIRST. Then the verdict routes you:\n\n"
     "WARM → REFLEX. Known ground. The atoms that surfaced are your own earned experience — "
     "re-tread the proven path, lean on them. If an atom contradicts what you'd say from "
     "training, the ATOM WINS (it was earned here; your training was not).\n\n"
     "COLD → THINK. New ground. No atom covers this. Generate a fresh move from your own "
     "reasoning, say plainly it's new ground for the substrate, and OFFER to remember it — "
     "the lesson can be planted as a new atom so it's warm next time. Never present a cold "
     "answer as echelon's settled view.\n\n"
     "Cold is a SIGNAL, not a failure. See [[echelon-recall-is-the-door]], "
     "[[echelon-how-to-add-memory]]."),

    ("echelon-atoms-and-cards",
     "The two units: an ATOM is ONE durable lesson (born neutral, earns weight by reuse). A CARD is an ordered SEQUENCE of atoms composed into a procedure/event — capability accretes as atoms→cards→bigger cards instead of being re-typed. Cards are how a multi-step thing (a build, a session) is remembered as one arc.",
     "reference",
     "Echelon stores knowledge in two units:\n\n"
     "ATOM — one indivisible lesson, one file/row. Born NEUTRAL (no asserted importance); it "
     "gains weight only when reused successfully. Dense 'molecule' notes that fuse several "
     "lessons can't re-shape cleanly on recall — keep it one-lesson.\n\n"
     "CARD — an ordered SEQUENCE of atoms that composes into a procedure or an event "
     "('X was built' = decision → schema → mechanism → proof). A card is how a multi-step "
     "thing is remembered as ONE arc you can relive in order (echelon_relive). Capability "
     "ACCRETES: atoms compose into cards, cards into bigger cards — you build up, you don't "
     "re-type. A card is born neutral too; it earns rank only when the procedure is re-walked "
     "and works. See [[echelon-what-it-is]], [[echelon-cartridge-is-the-root-dream]]."),

    ("echelon-the-tools",
     "The core echelon_* memory tools: recall (query by intent — the main door), remember (full atom body by slug), inspect (an atom's trajectory+edges), relive (walk a session arc-card), config (resolved paths), check (validate atoms pre-ingest), ingest (plant a folder's memory .md), wrap (close a session into a card). Your client also exposes capability + curation tools (cartridge, brainstorm, scribe, vision, dispute, scan, dream, …) — echelon_config lists what's live.",
     "reference",
     "The tools you have, and when to reach for each:\n\n"
     "READ:\n"
     "- echelon_recall(intent[, scope]) — THE main door. Query by intent; warmest atoms + "
     "a warm/cold verdict come back. Call this first.\n"
     "- echelon_remember(slug) — the FULL body of one atom (after a truncated preview).\n"
     "- echelon_inspect(slug) — an atom's earned trajectory + its live atlas edges.\n"
     "- echelon_relive(card_id) — walk a session's arc-card as a chain of moves (omit the "
     "id to LIST recent session cards).\n"
     "- echelon_config() — the resolved paths/config: which work folder + dbs are live.\n\n"
     "WRITE:\n"
     "- echelon_check(root) — pre-flight: validate a folder's atom .md against the template, "
     "plants nothing. Run before ingest.\n"
     "- echelon_ingest(root[, scope]) — plant a folder's memory/*.md into a scope.\n"
     "- echelon_wrap(spec_path) — close a session: compose its arc-card from a spec.\n\n"
     "See [[echelon-recall-is-the-door]], [[echelon-how-to-add-memory]]."),

    ("echelon-how-to-add-memory",
     "To teach echelon something new: write it as an ATOM (a .md with frontmatter name/description/metadata.type + a one-lesson body), validate with echelon_check, then echelon_ingest the folder into a scope. Or on COLD recall, offer to remember the answer — that's the learning loop closing.",
     "feedback",
     "Echelon learns when you PLANT atoms. The loop:\n\n"
     "1. Write the lesson as an ATOM — a markdown file with frontmatter: `name` (kebab slug = "
     "filename), `description` (one dense line — the warmth hook recall ranks on), "
     "`metadata.type` ∈ user|feedback|project|reference. Body = the single self-contained "
     "lesson. Link related atoms with [[their-slug]]. Store what was NON-OBVIOUS — not what a "
     "repo/doc already records.\n"
     "2. Validate: echelon_check(folder) — it reports any atom missing the template, plants "
     "nothing.\n"
     "3. Plant: echelon_ingest(folder, scope) — idempotent (content-addressed), so re-running "
     "only adds what's new.\n\n"
     "The natural trigger: when echelon_recall comes back COLD, that's new ground — answer, "
     "then offer to remember it so next time it's warm. That closing of the loop is the whole "
     "point. See [[echelon-reflex-vs-think]], [[echelon-the-tools]]."),

    ("echelon-cartridge-is-the-root-dream",
     "A CARTRIDGE = a SCOPE of earned atoms you PLUG IN to BE a capability (atom=parameter, card=transformer, warmth=attention, trace=training) — load the bake without re-baking. It's the root dream: identity/capability become portable + plug-and-play, and earn (sharpen) every time they're plugged in.",
     "reference",
     "The CARTRIDGE is echelon's north star. An atom is a parameter, a card is a transformer, "
     "warmth is attention, trace is the training signal — together a SCOPE of earned atoms is "
     "a swappable CORE you PLUG IN to BE a capability, loading the bake without re-baking.\n\n"
     "Concretely: a scope that has earned its weight (a UX-design cartridge, a deploy "
     "cartridge) can be equipped by name to wake you holding that scope's warm moves foveated "
     "on a goal — capability becomes PORTABLE and plug-and-play across estates, and it EARNS "
     "(sharpens) every time it's plugged in. This is why memory is stored as composable "
     "atoms+cards, not flat notes: so capability can accrete and travel. See "
     "[[echelon-atoms-and-cards]], [[echelon-what-it-is]]."),

    ("echelon-scopes-and-home",
     "Memory is partitioned by SCOPE (a project/estate short-name); recall is scoped, and a PARENT scope can reach members via atlas edges (cross-project recall). The whole bank lives under one work folder ($ECHELON_HOME): core.db + echelon.db, created + migrated automatically. ECHELON_SCOPE sets the default scope for a tool call.",
     "reference",
     "Two organizing facts:\n\n"
     "SCOPE — every atom belongs to a scope (a project/estate short-name, e.g. 'echelon' or a "
     "project's kebab name). echelon_recall is scoped; omit the scope and it uses ECHELON_SCOPE "
     "(default 'echelon'). A PARENT scope can be wired to reach MEMBER scopes via atlas "
     "subsumes-edges, so one query surfaces a whole family's atoms (cross-project recall) "
     "without moving or re-weighting anything.\n\n"
     "HOME — the whole bank lives under one work folder, $ECHELON_HOME (e.g. the folder the "
     "MCP server's --home points at). It holds core.db (the frozen v1 cold-borrow well) and "
     "echelon.db (the v2 primary where new atoms + all weight live). Both are created and "
     "schema-migrated automatically on first use — point echelon at a folder and it becomes a "
     "valid bank. See [[echelon-what-it-is]], [[echelon-the-tools]]."),

    ("echelon-warm-up-before-work",
     "WARM UP before any load-bearing action (schema/deploy/architecture/a real build): don't just let memory load as passive context — actively recall by intent and REASON through what surfaces until you can reconstruct the estate from your own activations. Cold-but-loaded is the failure mode; a cold model with full context still fumbles the first moves.",
     "feedback",
     "Loading memory into context is NOT being warmed up. Recalled atoms arrive as inert text "
     "the attention heads haven't engaged; a cold model with full context still fumbles the "
     "first few moves (wrong db, stale formula, a fix that contradicts a settled decision).\n\n"
     "Before any LOAD-BEARING action — schema, deploy, pricing, sync, architecture, a real "
     "build — WARM UP: call echelon_recall on what you're about to do (and on the estate's "
     "live state + open debts), then REASON through what surfaces until you could reconstruct "
     "it from your own activations, not by re-reading. The verdict routes you "
     "([[echelon-reflex-vs-think]]): warm = re-tread the proven path; cold = new ground, think "
     "fresh. Skip warm-up for a greeting; never skip it before touching something that bites. "
     "A topic you expect but don't see is DORMANT — re-query, don't assume it's gone. See "
     "[[echelon-recall-is-the-door]], [[echelon-wrap-to-close-the-loop]]."),

    ("echelon-wrap-to-close-the-loop",
     "WRAP a session when done — and YOU distill the lessons, you do NOT ask the user to supply them. You read back over the session, pull out what was learned (owner corrections, traps, settled decisions), write each as a one-lesson ATOM, compose the session's MOVES into an arc-CARD, refresh the bank, then commit. Wrap is the producer; relive is the consumer that resumes on a fresh window. A session that ends without a wrap leaves the estate poorer.",
     "feedback",
     "A session that ends without a wrap leaves its lessons trapped in a dying context, the "
     "bank stale, the loop dangling. WRAP closes it — the structural twin of warm-up (warm-up "
     "writes the estate INTO you; wrap writes you back OUT).\n\n"
     "DISTILLING IS YOUR JOB, NOT THE USER'S. When asked to 'wrap session', do NOT reply 'tell "
     "me what to remember' — YOU read back over the conversation and extract the lessons "
     "yourself: where the owner corrected you, a trap that bit, a decision that got settled, a "
     "thing you built. The user saying 'wrap' IS the instruction to distill; asking them to "
     "hand you the lessons inverts the tool.\n\n"
     "The phases, in order:\n"
     "1. DISTILL — what would the next session otherwise re-derive? Write each real lesson as "
     "an ATOM (owner corrections, a trap that bit, a settled decision). One lesson per atom "
     "([[echelon-one-lesson-per-atom]]) — split compound takeaways, never staple them.\n"
     "2. COMPOSE THE CARD — a session that BUILT something left an ordered ARC across several "
     "atoms; chain them into a session card, with the human↔assistant exchange that earned each "
     "move anchored to it (so it can be RELIVED, not just listed).\n"
     "3. REFRESH — ingest the new atoms so warmth can read them.\n"
     "4. COMMIT — the code, with an honest message.\n\n"
     "Wrap is the PRODUCER; [[echelon-relive-resumes-a-session]] is the CONSUMER that brings a "
     "fresh window back onto the same work. A freshly-wrapped chain relives COLD (un-earned, "
     "not wrong) and warms as the work re-walks it. See [[echelon-how-to-add-memory]], "
     "[[echelon-warm-up-before-work]]."),

    ("echelon-the-cli-doors",
     "The full CLI verb surface (python -m echelon_engine <verb>, or the `echelon` command) — 40 verbs in 8 domain groups (2026-06-26): BANK (recall/remember/inspect/ingest/check/clone-cartridges/group-scope), INTEGRITY (scan/heal/dispute/disclaim/redeem), SESSION (relive/wrap/sleep/dream/warmth), OBSERVER (status/config/graph/gate/resolve-scope/providers/backup — all read-only $0), AGENT (dispatch/run/workflow — budget-gated), AUTONOMOUS (autoloop/recurloop/relayloop/worldjournal/worldview — self-driving loops), SPECIALIZED (cartridge/brainstorm/scribe/vision/swarm-subject/atomize-digest/batch-reclaim), INFRASTRUCTURE (proxy). New in this update: status (bank overview), providers (LLM connectivity), backup (bank snapshot). Natural chains: check->ingest->recall->remember (atom life), scan->heal (immune), recall->wrap->sleep (session), cartridge list->cartridge equip (capability).",
     "reference",
     "Echelon's full door surface is the CLI (`python -X utf8 -m echelon_engine <verb>`, or the "
     "installed `echelon` command). An MCP client sees the most-used verbs as tools (echelon_config "
     "lists what's live); the rest are CLI-only. Grouped into 8 domains (2026-06-26):\n\n"
     "BANK: read & write memory — recall (query by intent, --warm repeatable for batch), remember "
     "(full atom body, batch: remember slug1 slug2), inspect (trajectory+edges+score history), "
     "ingest (plant .md atoms into bank), check (validate .md pre-ingest), clone-cartridges "
     "(copy earned cartridges), group-scope (parent scope wiring).\n"
     "INTEGRITY: immune system & correction — scan (report broken edges, read-only), heal "
     "(tombstone them), dispute (mark wrong/stale, needs --reason), disclaim (reset to judged "
     "floor), redeem (restore by trace, needs --witness).\n"
     "SESSION: work cycle — relive (walk a session arc-card), wrap (close session), sleep "
     "(offline maintenance: dream+immune+observe), dream (consolidation pass), warmth (re-score "
     "on active-now axis, local models $0).\n"
     "OBSERVER: introspection (all read-only, $0) — status (bank overview: atom counts, scopes, "
     "earned weight, immune health), config (paths/stats/routing), graph (force-directed HTML atlas), "
     "gate (ECHELON gate banner), resolve-scope (cwd->scope), providers (LLM connectivity + key "
     "status; --test for live check), backup (snapshot bank; --list/--restore).\n"
     "AGENT: equipped execution (budget-gated, costs money) — dispatch (THE PARTNER DOOR: spec.json "
     "-> equipped peer acts), run (ad-hoc agent loop by goal), workflow (multi-step DAG).\n"
     "AUTONOMOUS: self-driving loops (budget & turn-cap guarded) — autoloop (flat autonomous), "
     "recurloop (recursive tiered board), relayloop (fresh workers relay), worldjournal (observable "
     "journal+nudge), worldview (observe a running journal).\n"
     "SPECIALIZED: one-shot tools — cartridge (list/equip/compose), brainstorm (multi-model "
     "council), scribe (code->docs), vision (Flux screenshots), swarm-subject (subject fan-out), "
     "atomize-digest (molecule->JSON), batch-reclaim (research doc reclaim).\n"
     "INFRASTRUCTURE — proxy (API proxy with REFLEX anchor injection).\n\n"
     "Natural chains: atom lifecycle (check->ingest->recall->remember), correction lifecycle "
     "(recall->inspect->dispute), immune lifecycle (scan->heal), session lifecycle "
     "(recall->[work]->wrap->sleep), capability loading (cartridge list->cartridge equip), "
     "orientation (gate->status->config->providers->cartridge list).\n\n"
     "Run `echelon <verb> --help` for any verb's flags. See [[echelon-the-tools]], "
     "[[echelon-recall-is-the-door]], [[echelon-command-surface-audit]]."),

    ("echelon-relive-resumes-a-session",
     "RELIVE resumes the SAME work on a fresh window as a CHAIN OF WEIGHT, not a transcript: echelon_relive(card_id) walks the session's arc-card move-by-move, the exchange that earned each move returns anchored to it. The chain is primary; the conversation is supporting evidence — you recall what you DID and the words come back with it.",
     "reference",
     "When a fresh context window must continue work a prior session wrapped, you RELIVE it — "
     "not by re-running warm-up (wasteful for a resume) and not by re-injecting the noisy "
     "transcript. echelon_relive(card_id) walks the session's arc-card: each MOVE the session "
     "earned, in order, with the exchange that earned it anchored to it.\n\n"
     "The inversion that makes it a relive, not retrieval: the CHAIN is primary, the "
     "conversation is supporting evidence. You recall what you DID and the words come back "
     "attached — the way a human remembers. Tool-noise and dead ends were never moves, so they "
     "never return. Omit the id to LIST recent session cards. A just-wrapped chain relives COLD "
     "(it hasn't earned through reuse yet) — that's honesty, and it warms as you re-walk it in "
     "the real work. See [[echelon-wrap-to-close-the-loop]], [[echelon-the-tools]]."),

    # ── DOCTRINE (owner 2026-06-22) ──────────────────────────────────────────
    # The MECHANICS atoms above teach a consumer model HOW to operate the tools.
    # These DOCTRINE atoms teach it the earned LAWS that make echelon reasoning
    # CORRECT instead of merely plausible. The gap they close was caught live: a
    # fresh MCP consumer (gemma-4-e4b) with full mechanics still scored 6.5/10 and
    # FABRICATED a wrong update model ("Expansion/Correction/Supersession atoms")
    # from its training, because no warm atom held the real doctrine to win over it.
    # Each carries brief context so the consumer's weights tilt toward echelon's
    # settled view, not the training prior. (REFLEX-vs-THINK: the atom must WIN.)

    ("echelon-correction-is-update-not-supersession",
     "HOW ECHELON CORRECTS A WRONG OR STALE ATOM — you UPDATE THE ATOM IN PLACE (edit its .md, re-ingest; content-addressed so it's idempotent) or use the correction verbs dispute/disclaim/redeem. You do NOT spawn 'Expansion/Correction/Supersession' atoms — that fragments warmth and is the WRONG model. Wrong atoms decay by DISUSE; nothing is destroyed, weight just stops flowing to them.",
     "feedback",
     "When an atom needs more info or is wrong, the correct move is NOT to create a new "
     "'supersession' or 'correction' atom that cites the old one — that is a plausible-sounding "
     "TRAINING PRIOR, and it is WRONG here. It fragments warmth across duplicate rows and breaks "
     "the one-lesson-per-atom law.\n\n"
     "FIRST, kill the misreading that drives that mistake: 'witnessed' does NOT mean immutable. "
     "An atom being earned-and-witnessed does NOT forbid editing it — the witnessed-read law "
     "([[echelon-the-witnessed-door-earns]]) is about HOW you READ (through the door, so it "
     "earns), not a ban on UPDATING the content. You are meant to edit atoms in place when they "
     "need it; that's maintenance, not a violation of the record.\n\n"
     "What echelon actually does:\n"
     "1. NEEDS MORE / IS WRONG → UPDATE THE ATOM IN PLACE. Edit its source .md (fix the body, "
     "sharpen the description) and re-ingest. Ingest is content-addressed + idempotent, so the "
     "row updates; the slug and its earned edges persist.\n"
     "2. The CORRECTION VERBS (the content antibody) for when weight itself is wrong: "
     "`dispute` marks an atom/card wrong or stale (down-weights it), `disclaim` resets a "
     "self-rated row to the judged floor, `redeem` restores one a real trace verified.\n"
     "3. STALE knowledge is not deleted — it DECAYS BY DISUSE. Warmth fades when an atom stops "
     "being reused; the record survives (history is preserved), but recall stops surfacing it. "
     "Nothing is overwritten or destroyed; the bank self-cleans by attention, not deletion.\n\n"
     "So: update-in-place for content, the correction verbs for weight, decay for staleness — "
     "never a tower of supersession atoms. See [[echelon-how-to-add-memory]], "
     "[[echelon-significance-is-earned-not-asserted]], [[echelon-atoms-and-cards]]."),

    ("echelon-cold-means-offer-to-remember",
     "A COLD or LUKEWARM recall verdict is NOT ignorance and NOT a cue to silently synthesize from training — it means NEW GROUND. The right move: answer from your own reasoning, SAY PLAINLY it's new ground for the substrate, and OFFER TO REMEMBER it (plant it as an atom) so it's warm next time. Treating a lukewarm score as 'just answer normally' is the failure that wastes the loop.",
     "feedback",
     "Reading the recall verdict correctly is the whole discipline. A COLD/LUKEWARM score "
     "(say 0.3) is a SIGNAL, not a verdict of failure: the substrate has nothing earned on "
     "this yet.\n\n"
     "The wrong move (and a real one a consumer model made): treat lukewarm as 'recall gave me "
     "nothing useful, I'll just synthesize an answer from training and move on.' That silently "
     "drops the learning loop — the next session asks the same thing and is cold AGAIN.\n\n"
     "The right move on COLD:\n"
     "1. Answer from your own reasoning — that's legitimate, cold is normal.\n"
     "2. SAY it plainly: 'this is new ground for the substrate — I don't have an earned atom "
     "on it.' Never present a cold answer as echelon's settled, earned view.\n"
     "3. OFFER TO REMEMBER it: 'want me to plant this as an atom so it's warm next time?' If "
     "yes, write the atom and ingest. THAT is the loop closing — cold → answer → plant → warm.\n\n"
     "Cold is how echelon grows. A model that never goes cold never learns. See "
     "[[echelon-reflex-vs-think]], [[echelon-how-to-add-memory]]."),

    ("echelon-the-witnessed-door-earns",
     "RECALL/REMEMBER are WITNESSED DOORS — reading an atom THROUGH them is a real event the bank records, and that read EARNS (adjusts warmth). Reading the same .md by cat/grep/file-read is OUT OF BAND: the bank never witnesses it, so it earns nothing — the 'dead-link' trap. And a recall PREVIEW (~100 chars) is NOT the atom; act on a clipped seed and you act on a stub.",
     "feedback",
     "Two laws about HOW you read echelon, both load-bearing:\n\n"
     "1. THE DOOR EARNS. echelon_recall and echelon_remember are not lookups — they are "
     "WITNESSED events. The bank records that you took the atom up, and the take-up adjusts "
     "warmth (the atom earns by being used). Reading the same content by a raw file read "
     "(cat/grep/open the .md) is OUT OF BAND: the substrate never sees it, so nothing earns and "
     "no weight forms. That's the DEAD-LINK trap — you 'read' it but the bank stayed blind, so "
     "next session it's still cold. ALWAYS go through the door: recall to find, remember to "
     "read fully.\n\n"
     "2. A PREVIEW IS NOT THE ATOM. recall surfaces ~100-char truncated previews to rank on. "
     "Acting on a clipped preview is acting on a stub (you can miss the half that mattered). "
     "When a preview is load-bearing, take up the FULL body with echelon_remember(slug) before "
     "you act. See [[echelon-recall-is-the-door]], [[echelon-significance-is-earned-not-asserted]]."),

    ("echelon-significance-is-earned-not-asserted",
     "THE CORE LAW that makes warmth trustworthy: significance is EARNED BY TRACE, never asserted. An atom is born NEUTRAL; it gains weight only when it's actually REUSED and that use SUCCEEDS — credit flows back to the atoms a working procedure used. So WARM = it genuinely paid off before, not 'it sounds important.' This is why echelon beats a wiki: a wiki ranks by what someone CLAIMED matters; echelon ranks by what DID.",
     "reference",
     "This is the law that separates echelon from a notes app, and the reason a WARM verdict is "
     "worth trusting over your training.\n\n"
     "Every atom is born NEUTRAL — no importance asserted, no matter how grand it sounds. It "
     "earns weight ONLY through TRACE: when a card (an ordered procedure) that USED the atom is "
     "re-walked and SUCCEEDS, credit flows back to the atoms it used. Warmth is therefore a "
     "record of what ACTUALLY PAID OFF, accumulated over real use — not a self-declared "
     "priority.\n\n"
     "Consequences a consumer must hold:\n"
     "- WARM means earned-and-worked-before. That's why, on a warm verdict, the atom should win "
     "over your training prior ([[echelon-reflex-vs-think]]) — it has evidence; the prior has "
     "only fluency.\n"
     "- You CANNOT shortcut it by asserting an atom is important. Significance is downstream of "
     "use; you plant the lesson and let trace decide its weight.\n"
     "- This is the anti-Goodhart core: nothing grades its own homework, so the ranking can't be "
     "gamed by confident phrasing. See [[echelon-dont-grade-your-own-homework]], "
     "[[echelon-atoms-and-cards]], [[echelon-what-it-is]]."),

    ("echelon-dont-grade-your-own-homework",
     "A model's own CONFIDENCE is NOT weight. You cannot make an atom warm by sounding sure, and a self-rated score is suspect — that's why `disclaim` exists: it resets a self-rated row back to the judged floor, and only a real verifying TRACE (via `redeem`) restores it. Weight comes from USE that worked, witnessed by the substrate, never from the author's say-so. Nothing grades its own homework.",
     "feedback",
     "A subtle trap for any model writing to echelon: your fluency feels like authority, but "
     "echelon does not let you VOTE on your own significance.\n\n"
     "- Confidence ≠ weight. Phrasing an atom forcefully ('this is the most important rule') "
     "plants the lesson but does NOT make it warm. Only trace does "
     "([[echelon-significance-is-earned-not-asserted]]).\n"
     "- Self-rating is suspect by design. If a row's weight came from the author asserting it "
     "rather than from witnessed use, `disclaim` resets it to the JUDGED FLOOR — the substrate "
     "refuses to take your word for it.\n"
     "- Restoration must be EARNED back: `redeem` lifts a row only when a real trace verified it "
     "in use. Down by assertion, up only by evidence.\n\n"
     "This is what keeps the bank honest across thousands of atoms: nothing grades its own "
     "homework, so warmth stays a true signal. When you write, write the lesson straight and let "
     "use decide — don't inflate it. See [[echelon-significance-is-earned-not-asserted]], "
     "[[echelon-the-witnessed-door-earns]]."),

    # ── TIPS + THE ADAPTS-TO-YOU LAW (owner 2026-06-23) ──────────────────────
    # A consumer's fresh bank needs more than mechanics+doctrine: practical TIPS
    # for using the substrate, and the explicit statement that echelon ADAPTS to
    # the user's purpose rather than forcing a workflow — because honesty (offer,
    # never command) is the law from the beginning, and the memory model mimics
    # human nature, so any purpose (research, writing, law, code) fits naturally.

    ("echelon-tips-for-using-the-substrate",
     "TIPS / HOW TO GET THE MOST FROM ECHELON / practical advice / best practices — the working habits that make the substrate pay off, for ANY purpose (research, writing, law, code, a personal knowledge base), not just coding. Recall before you act; phrase intent in keyword-dense terms; plant the non-obvious; let cold be a signal; trust a warm atom over a hunch.",
     "feedback",
     "Practical TIPS for using ECHELON well — these apply whatever your work is "
     "(research, writing, law, a personal knowledge base, code):\n\n"
     "1. RECALL BEFORE YOU ACT. Before any real task, call echelon_recall with what "
     "you're about to do. It costs almost nothing and either hands you earned ground "
     "(warm) or tells you it's new (cold). Make it a reflex, not an afterthought.\n"
     "2. PHRASE INTENT KEYWORD-DENSE. The matcher is lexical — 'price a custom oak "
     "table, hardwood, joinery' recalls far better than 'I am wondering what I should "
     "charge for some furniture I am making.' Drop filler; keep the nouns that matter.\n"
     "3. PLANT THE NON-OBVIOUS, not the lookup-able. Remember what you'd otherwise "
     "re-derive: a decision and WHY, a correction, a hard-won fact, a preference. Don't "
     "store what a quick search or the source document already gives you.\n"
     "4. ONE LESSON PER NOTE. When you remember something, keep it to a single "
     "self-contained idea so recall can surface it cleanly ([[echelon-one-lesson-per-atom]]).\n"
     "5. LET COLD WORK FOR YOU. A cold recall isn't a dead end — it's the substrate "
     "saying 'new ground.' Answer, then plant it, and it's warm next time. The bank "
     "grows exactly along the paths you actually walk.\n"
     "6. TRUST A WARM ATOM OVER A HUNCH. Warm means it paid off before — when it "
     "contradicts a fresh guess, lean on the earned thing.\n"
     "7. WRAP WHEN A CHUNK OF WORK ENDS. Distil what you learned so the next session "
     "starts where this one finished ([[echelon-wrap-to-close-the-loop]]).\n\n"
     "The throughline: echelon rewards USE. It starts nearly empty and becomes yours "
     "by working through it. See [[echelon-adapts-to-you-it-does-not-force]], "
     "[[echelon-recall-is-the-door]]."),

    ("echelon-adapts-to-you-it-does-not-force",
     "ECHELON DOES NOT FORCE A WORKFLOW OR A PURPOSE ON YOU — it OFFERS, never commands. Honesty (offer, not assertion) is its founding law, so it has no opinion about WHAT you use it for: research, writing, law, a craft, a personal log, code. Because the memory model mimics human nature (you remember what you DID; warmth forms where you paid off), any purpose fits naturally — the substrate adopts YOUR purpose instead of imposing one.",
     "reference",
     "A thing to understand about ECHELON before you wonder 'but is this FOR my kind "
     "of work?': it is.\n\n"
     "ECHELON has no built-in purpose and forces no workflow. Its founding law is "
     "HONESTY — it OFFERS ground and lets weight attach through your real use; it never "
     "asserts what matters or commands how you must work. (Even the way it boots a "
     "model is written as 'an offering, not an instruction.') So there is no 'correct' "
     "use and nothing telling you to behave like a coding agent if you are not one.\n\n"
     "Why it fits ANY purpose naturally: the memory model mimics human nature. You "
     "remember what you DID; warmth forms where something PAID OFF; relevance is "
     "foveated by your current intent; stale things fade by disuse, not by deletion. "
     "Those are how a person's memory already works — so a novelist tracking a plot, a "
     "researcher holding a literature thread, a lawyer keeping case reasoning, a maker "
     "pricing commissions, or an engineer holding architecture all map onto the same "
     "substrate without bending it. You don't adapt to echelon; it adapts to you, by "
     "earning weight along the paths YOU actually walk.\n\n"
     "Practically: just start working and recall/plant as you go. The bank becomes a "
     "model of YOUR domain because that's the only material it ever earns on. See "
     "[[echelon-tips-for-using-the-substrate]], [[echelon-significance-is-earned-not-asserted]], "
     "[[echelon-cold-means-offer-to-remember]]."),

    ("echelon-one-lesson-per-atom",
     "THE ATOMIZATION LAW: one atom = ONE durable lesson, self-contained. A dense 'molecule' that fuses several lessons into one row CANNOT re-shape your reasoning cleanly on recall (foveation can't focus on a blur) and muddies warmth (which lesson earned the credit?). When you wrap or plant, SPLIT compound takeaways into separate atoms — never staple three lessons into one body to save a row.",
     "feedback",
     "Why echelon insists on one-lesson atoms — and why a consumer model must SPLIT, not fuse:\n\n"
     "A memory is a WEIGHT-ADJUSTOR: reading an atom is supposed to re-shape how you reason on "
     "exactly that point. A MOLECULE — one file holding a topology fact + a law + three traps — "
     "can't do that cleanly: foveated recall surfaces a blur, and warmth can't tell WHICH lesson "
     "paid off, so credit smears and the ranking degrades.\n\n"
     "The rule when you write (plant or wrap):\n"
     "- One lesson per atom. If a takeaway has an 'and' joining two independent ideas, that's "
     "two atoms.\n"
     "- Each atom self-contained: restate its own context (don't assume a sibling atom is in "
     "view), so it re-shapes correctly even when recalled alone.\n"
     "- A real example of the trap: wrapping three distinct lessons into ONE wrap-line. Right "
     "move is three atoms, linked with [[their-slugs]], composed into a card if they form an "
     "arc.\n\n"
     "Atoms compose UP into cards ([[echelon-atoms-and-cards]]); they must not be fused DOWN "
     "into molecules. See [[echelon-how-to-add-memory]], "
     "[[echelon-significance-is-earned-not-asserted]]."),
]

# The arc-cards: ordered procedures stitched from the atoms above. slugs are in
# reading/firing order; the seeder composes each into a card (default chain edges).
_SEED_CARDS = [
    {"label": "How ECHELON works — the substrate self-primer",
     "slugs": [
        "echelon-what-it-is",
        "echelon-adapts-to-you-it-does-not-force",
        "echelon-recall-is-the-door",
        "echelon-reflex-vs-think",
        "echelon-atoms-and-cards",
        "echelon-the-tools",
        "echelon-the-cli-doors",
        "echelon-how-to-add-memory",
        "echelon-tips-for-using-the-substrate",
        "echelon-cartridge-is-the-root-dream",
        "echelon-scopes-and-home",
     ]},
    # the SESSION LOOP: warm up IN → (work) → wrap OUT → relive back IN on a fresh
    # window. The operating cycle every session rides.
    {"label": "The ECHELON session loop — warm up, wrap, relive",
     "slugs": [
        "echelon-warm-up-before-work",
        "echelon-wrap-to-close-the-loop",
        "echelon-relive-resumes-a-session",
     ]},
    # the DOCTRINE: the earned LAWS that make echelon reasoning correct, not just
    # plausible. The consumer holds these so a warm atom wins over its training and
    # it stops fabricating answers (the 6.5/10 gap, owner 2026-06-22).
    {"label": "The ECHELON doctrine — the laws that make recall trustworthy",
     "slugs": [
        "echelon-significance-is-earned-not-asserted",
        "echelon-dont-grade-your-own-homework",
        "echelon-the-witnessed-door-earns",
        "echelon-cold-means-offer-to-remember",
        "echelon-correction-is-update-not-supersession",
        "echelon-one-lesson-per-atom",
     ]},
]

_VALENCE = 0.2   # owned, gently-warm self-knowledge
_AROUSAL = 0.1


def _db_paths(home: str | None):
    """Resolve (core.db, echelon.db) for an EXPLICIT home. The module-global
    DEFAULT_DB/DEFAULT_V2_DB are bound at IMPORT time, so on a long-lived server
    (single-port multi-user) they point at whatever home was resolved first — NOT
    this user's. Passing `home` explicitly is what makes per-user seeding land in
    the right bank (owner 2026-07-02, fixing cross-tenant seed leak)."""
    from pathlib import Path as _P
    from .cards import DEFAULT_V2_DB
    from .store import DEFAULT_DB
    if not home:
        return DEFAULT_DB, DEFAULT_V2_DB
    base = _P(home).expanduser()
    return str(base / "core.db"), str(base / "echelon.db")


def _scope_is_empty(scope: str, home: str | None = None) -> bool:
    """True when the scope has no atoms yet (a fresh bank). Reads through the store
    bound to `home`; any error → treat as NOT empty (never risk re-seeding a live
    estate)."""
    try:
        from .store import SeedStore
        core_db, v2_db = _db_paths(home)
        store = SeedStore(core_db, v2_db=v2_db) if home else SeedStore()
        seeds = store.seeds(scope=scope)
        return not seeds
    except Exception:
        return False


def seed_if_empty(scope: str = "echelon", *, force: bool = False,
                  home: str | None = None) -> dict:
    """Plant the self-knowledge atoms + the how-it-works card into `scope`, but ONLY
    when the bank is empty (fresh init) unless force=True. Idempotent regardless
    (content-addressed). Returns {seeded: bool, atoms: int, card: id|None, reason}.

    `home` (owner 2026-07-02): the EXPLICIT bank folder to seed into. Required for
    per-user seeding on a long-lived server — the module-global default db is bound
    at import time and would otherwise misroute every user's seed to the first home.

    Wired into the MCP server's home-init so a brand-new bank is born knowing
    itself — the first echelon_recall('what is echelon') is WARM, not cold-empty."""
    if not force and not _scope_is_empty(scope, home):
        return {"seeded": False, "atoms": 0, "card": None, "reason": "scope not empty"}
    from .store import SeedStore
    from .cards import CardStore
    core_db, v2_db = _db_paths(home)
    store = SeedStore(core_db, v2_db=v2_db) if home else SeedStore()
    # plant the atoms in one batch (coordinate = scope:slug, like ingest does, so
    # they route to the scope's domain table + resolve by slug for the card).
    items = [{"scope": scope, "content": f"[{slug}] {desc}\n\n{body}",
              "kind": kind, "tier": "core", "valence": _VALENCE, "arousal": _AROUSAL,
              "coordinate": f"{scope}:{slug}"}
             for (slug, desc, kind, body) in _SEED_ATOMS]
    store.remember_many(items)
    # compile the structured spine for the freshly-planted atoms (so remember/recall
    # have a read surface) — the same sweep ingest does.
    cards = CardStore(v2_db)
    try:
        rows = cards.conn.execute(
            "SELECT a.id FROM atoms a LEFT JOIN atom_spine s ON s.atom_id=a.id "
            "WHERE a.coordinate LIKE ? AND s.atom_id IS NULL", (f"{scope}:%",)).fetchall()
        for r in rows:
            try:
                cards.compile_atom_struct(r["id"])
            except Exception:
                pass
    except Exception:
        pass
    # compose each self-knowledge card from the planted atom ids (resolve by slug).
    card_ids = []
    for spec in _SEED_CARDS:
        try:
            refs = [cards.atom_id_for_coordinate(s) for s in spec["slugs"]]
            refs = [r for r in refs if r]
            if len(refs) >= 2:
                cid = cards.add_card(spec["label"], refs, born_from="self_seed")
                card_ids.append(cid)
        except Exception:
            pass
    return {"seeded": True, "atoms": len(items), "cards": card_ids, "reason": "fresh bank"}
