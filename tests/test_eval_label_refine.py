"""Y1 L3 — gold promotion: cross-vendor agreement (with independence proof) +
search-refine merge."""
from __future__ import annotations

import pytest

from event_intel.eval import label_refine as RF


def _drafted(name, sug, needs=False):
    return {"index": 0, "name": name, "overview": f"{name} overview", "url": None,
            "label": "", "suggested_label": sug, "confidence": 0.9,
            "rationale": "r", "source": "gpt_draft", "needs_review": needs}


def _good_sha(rows):
    return RF.input_sha(RF.independent_input_view(rows))


# ---------- independence view + sha ----------

def test_independent_view_strips_gpt_fields():
    rows = [_drafted("Acme", "competitor")]
    view = RF.independent_input_view(rows)
    assert set(view[0]) == {"name", "overview", "url"}  # no suggested_label/confidence/rationale


# ---------- cross-vendor agreement ----------

def test_agreement_promotes_to_gold():
    rows = [_drafted("Acme", "competitor"), _drafted("Globex", "target")]
    out = RF.merge_cross_vendor(
        rows, {"Acme": "competitor", "Globex": "target"},
        independent_input_sha=_good_sha(rows), prompt_sha="p", model_id="claude",
    )
    by = {r["name"]: r for r in out}
    assert by["Acme"]["grade"] == "gold" and by["Acme"]["source"] == "cross_agree"
    assert by["Acme"]["adjudicators"] == ["gpt_draft", "claude_independent"]
    assert by["Acme"]["independence"]["model_id"] == "claude"


def test_disagreement_flags_not_gold():
    rows = [_drafted("Acme", "competitor")]
    out = RF.merge_cross_vendor(
        rows, {"Acme": "neutral"},  # disagree
        independent_input_sha=_good_sha(rows), prompt_sha="p", model_id="claude",
    )
    assert out[0]["needs_review"] is True and out[0]["grade"] == ""


def test_missing_claude_label_flags():
    rows = [_drafted("Acme", "competitor")]
    out = RF.merge_cross_vendor(
        rows, {}, independent_input_sha=_good_sha(rows), prompt_sha="p", model_id="claude",
    )
    assert out[0]["needs_review"] is True and out[0]["grade"] == ""


def test_refuses_when_input_sha_proves_gpt_leak():
    """If the SHA isn't the GPT-blind view's SHA (e.g. the 2nd vendor saw GPT
    fields), gold promotion is refused (review R2#5)."""
    rows = [_drafted("Acme", "competitor")]
    # sha of a view that wrongly INCLUDES the gpt suggestion
    leaky = RF.input_sha([{"name": "Acme", "overview": "o", "suggested_label": "competitor"}])
    with pytest.raises(ValueError, match="independent"):
        RF.merge_cross_vendor(
            rows, {"Acme": "competitor"},
            independent_input_sha=leaky, prompt_sha="p", model_id="claude",
        )


# ---------- search refine ----------

def test_apply_refinements_promotes_flagged_only():
    rows = [_drafted("Acme", "competitor", needs=True),   # flagged → refine
            _drafted("Globex", "target", needs=False)]    # not flagged → untouched
    out = RF.apply_refinements(rows, {
        "Acme": {"final_label": "neutral", "evidence_urls": ["https://acme"], "note": "actually a reseller"},
        "Globex": {"final_label": "bad_fit"},  # not flagged → ignored
    })
    by = {r["name"]: r for r in out}
    assert by["Acme"]["final_label"] == "neutral" and by["Acme"]["grade"] == "gold"
    assert by["Acme"]["source"] == "search_refine" and by["Acme"]["search_evidence"] == ["https://acme"]
    assert by["Acme"]["refine_note"] == "actually a reseller"
    # Globex was not flagged → untouched (no gold, no final_label change)
    assert by["Globex"].get("grade") != "gold" and "final_label" not in by["Globex"]


def test_apply_refinements_rejects_invalid_label():
    rows = [_drafted("Acme", "competitor", needs=True)]
    with pytest.raises(ValueError, match="invalid"):
        RF.apply_refinements(rows, {"Acme": {"final_label": "rival"}})


# ---------- provenance reaches sealed labels ----------

def test_refine_provenance_flows_to_sealed():
    from event_intel.eval import blind as BL
    from event_intel.eval import labeling as L

    rows = [_drafted("Acme", "competitor", needs=True)]
    refined = RF.apply_refinements(rows, {"Acme": {"final_label": "competitor", "evidence_urls": ["https://x"]}})
    labels, grades, prov = L.extract_sealed_inputs(refined)
    assert grades["Acme"] == "gold"
    assert prov["Acme"]["source"] == "search_refine" and prov["Acme"]["search_evidence"] == ["https://x"]
    pkt = BL.CompanyPacket(pair="p", cohort=BL.FULL, seed=0, entries=[{"index": 0, "name": "Acme"}])
    sealed = BL.seal_company_labels(pkt, labels, grades=grades, provenance=prov)
    assert sealed.grades["Acme"] == "gold"
    assert sealed.provenance["Acme"]["search_evidence"] == ["https://x"]


# ---------- L4 label-stats ----------

def test_label_stats_rates():
    rows = [
        {"name": "A", "suggested_label": "target", "final_label": "target",
         "grade": "silver", "source": "gpt_draft", "needs_review": False},
        {"name": "B", "suggested_label": "competitor", "final_label": "competitor",
         "grade": "gold", "source": "cross_agree", "needs_review": False},
        {"name": "C", "suggested_label": "target", "final_label": "neutral",
         "grade": "gold", "source": "search_refine", "needs_review": False},  # flipped
        {"name": "D", "suggested_label": "bad_fit", "final_label": "",
         "grade": "", "needs_review": True},  # still flagged
    ]
    s = RF.label_stats(rows)
    assert s["n"] == 4
    assert s["by_grade"]["gold"] == 2 and s["by_grade"]["silver"] == 1 and s["by_grade"]["ungraded"] == 1
    assert s["gold_rate"] == 0.5 and s["cross_agree_rate"] == 0.25
    assert s["flag_rate"] == 0.25 and s["flip_rate"] == 0.25  # only C flipped
    assert s["by_source"]["search_refine"] == 1


def test_label_stats_empty():
    s = RF.label_stats([])
    assert s["n"] == 0 and s["gold_rate"] == 0.0
