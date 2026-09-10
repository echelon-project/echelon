"""swarm response contract — declare the shape once, ingest BY KEY.

WHAT THIS REPLACED, and why the tests are shaped this way. The worker's output shape used to
live in two places: a prose description inside the prompt, and a regex that scraped the shape
back out of the answer. The regex could not express nesting —

    re.search(r'\\{[^{}]*"ok"\\s*:\\s*(true|false)[^{}]*\\}', raw, re.S)

`[^{}]` cannot cross a nested brace, and every real report carries `findings: [{...}]`, so an
unfenced response had ALL its findings discarded and was recorded as `[✗] 0 findings`. Measured
against six real live responses on 2026-08-18, that parser discarded **6 of 6** — 17 findings
destroyed, every lens reported as failed. The models had done their jobs.

So the tests below assert two separable things:
  * the contract INGESTS every real-world response shape (the regression surface), and
  * a failure is never reported as an empty success (the trust surface).
"""
import json

import pytest

from echelon_engine.swarm import contract as C


# ── the shapes the old regex silently destroyed ───────────────────────────────

@pytest.mark.parametrize("body,label", [
    ('{"ok":true,"findings":[{"title":"t"}]}', "bare JSON (json_object mode)"),
    ('Analysis follows.\n{"ok":true,"findings":[{"title":"t"}]}', "prose then JSON"),
    ('```\n{"ok":true,"findings":[{"title":"t"}]}\n```', "fence without a language tag"),
    ('```json\n{"ok":true,"findings":[{"title":"t"}]}\n```', "fenced json"),
    ('{"ok":true,"findings":[{"title":"t","detail":"has {braces} inside"}]}', "braces in a value"),
    ('{"ok":true,"findings":[{"title":"a } b"}]}', "close-brace inside a string"),
])
def test_ingests_every_response_shape(body, label):
    """Each of these carries exactly one finding. The old parser returned ok=False and zero
    findings for the unfenced ones — a worker that succeeded, recorded as a failed lens."""
    r = C.ingest(body)
    assert r["ok"] is True, f"{label}: must parse"
    assert len(r["findings"]) == 1, f"{label}: the finding must survive"
    assert r["parse_error"] == ""


def test_the_old_regex_is_not_reintroduced():
    """The exact pattern that caused the loss, pinned as a refuter. If someone reaches for a
    regex again, this documents what it cannot do: nesting is not a regular language."""
    import re
    old = re.compile(r'\{[^{}]*"ok"\s*:\s*(true|false)[^{}]*\}', re.S)
    nested = '{"ok":true,"findings":[{"title":"t"}]}'
    assert old.search(nested) is None, "the old pattern cannot match nested findings — that was the bug"
    assert C.ingest(nested)["findings"], "the contract must handle what the regex could not"


# ── the trust surface: a failure must never read as an empty success ──────────

def test_unparseable_response_is_a_failure_not_an_empty_result():
    """The conflation that let a dead lens read as a clean bill of health: both produced
    'zero findings'. A parse failure must carry ok=False AND a reason."""
    r = C.ingest("I could not complete this task.")
    assert r["ok"] is False
    assert r["parse_error"], "a failure must say WHY, or it is indistinguishable from silence"


def test_genuinely_empty_result_is_a_success():
    """The other half: a lens that ran and found nothing is a SUCCESS with zero findings.
    Only this distinction makes 'no findings' meaningful."""
    r = C.ingest('{"ok":true,"findings":[]}')
    assert r["ok"] is True and r["findings"] == [] and r["parse_error"] == ""


def test_junk_rows_are_counted_not_silently_dropped():
    """A malformed finding is reported as dropped. Silently skipping it makes 'the model
    emitted junk' look identical to 'the model found less'."""
    r = C.ingest('{"ok":true,"findings":["not an object",{"title":""},{"title":"real"}]}')
    assert len(r["findings"]) == 1
    assert r.get("dropped_findings") == 2


# ── coercion: a wrong type is corrected, never dropped ────────────────────────

def test_wrong_types_are_coerced_to_the_declared_shape():
    r = C.ingest('{"ok":"yes","findings":[{"title":"t","severity":"critical"}]}')
    assert r["ok"] is True                       # "yes" -> True
    f = r["findings"][0]
    assert f["severity"] == "LOW"                # "critical" is not in the enum -> default


# ── THE VERDICT LAW (2026-08-18) — replaced the `confidence` float ────────────
# A number is a feeling a worker assigns itself and nothing downstream can check a 0.8;
# CONFIRMED/PLAUSIBLE is a claim about WHAT THE WORKER DID, checkable against `evidence`.
# These tests are the doctrine's code-followers: if the enum ever admits a third tier or
# stops defaulting to PLAUSIBLE, the law is silently gone and these must fail.

def test_confidence_is_gone_from_the_contract():
    """The retired field must not come back as a shadow default."""
    r = C.ingest('{"ok":true,"findings":[{"title":"t","confidence":0.9}]}')
    assert "confidence" not in r["findings"][0]


def test_an_unlabelled_finding_is_plausible_never_hard_truth():
    """The whole point of the tier: silence must never read as witnessed fact."""
    r = C.ingest('{"ok":true,"findings":[{"title":"t"}]}')
    assert r["findings"][0]["verdict"] == "PLAUSIBLE"


def test_a_worker_cannot_invent_a_middle_tier():
    """Only two labels. 'PROBABLY' is not a hedge the contract will honour."""
    r = C.ingest('{"ok":true,"findings":[{"title":"t","verdict":"PROBABLY"}]}')
    assert r["findings"][0]["verdict"] == "PLAUSIBLE"


def test_verdict_case_is_normalised():
    r = C.ingest('{"ok":true,"findings":[{"title":"t","verdict":"confirmed"}]}')
    assert r["findings"][0]["verdict"] == "CONFIRMED"


def test_missing_fields_take_contract_defaults():
    r = C.ingest('{"ok":true,"findings":[{"title":"only a title"}]}')
    f = r["findings"][0]
    assert f["severity"] == "LOW" and f["category"] == "other"
    assert f["detail"] == "" and f["evidence"] == "" and f["fix"] == ""
    assert r["recommendations"] == [] and r["risks"] == []


# ── one declaration feeds both ends ───────────────────────────────────────────

def test_prompt_spec_is_generated_from_the_contract():
    """The spec sent to the model must be DERIVED from the contract, not hand-written beside
    it — two copies of one shape drift, and that drift was the original defect."""
    spec = C.prompt_spec()
    c = C.load()
    for key in c["fields"]:
        assert f'"{key}"' in spec, f"{key} is declared but never asked for"
    for key in c["fields"]["findings"]["item"]["fields"]:
        assert f'"{key}"' in spec


def test_contract_example_satisfies_its_own_contract():
    """The example shipped in the contract is what the model imitates. If it does not
    round-trip through the ingest, the contract is teaching a shape it will then reject."""
    example = C.load()["example"]
    r = C.ingest(json.dumps(example))
    assert r["ok"] is True
    assert len(r["findings"]) == len(example["findings"])
    assert r["parse_error"] == ""


def test_response_format_requests_json_mode():
    """The provider is asked for a JSON document so the answer is well-formed by
    construction, rather than being scraped out of prose afterwards."""
    assert C.response_format() == {"type": "json_object"}
