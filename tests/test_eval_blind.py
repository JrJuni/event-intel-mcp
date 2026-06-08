"""Y1 CS3/CS3b — blind company packet (cohorts), sealed labels, evidence packet
ordering. Asserts the blind boundary is enforced at the artifact level."""
from __future__ import annotations

import pytest

from event_intel.eval import blind as B
from event_intel.eval.roster import RosterEntry

_ROSTER = [RosterEntry(f"r{i}", f"Company {i}") for i in range(20)]


# ---------- company packet: cohorts + blindness ----------

def test_full_cohort_packet_covers_whole_roster():
    p = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=7)
    assert len(p.entries) == len(_ROSTER)
    assert {e["name"] for e in p.entries} == {e.canonical_name for e in _ROSTER}


def test_packet_entries_carry_no_engine_output():
    p = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=1)
    for e in p.entries:
        assert set(e.keys()) == {"index", "name"}  # no score / tier / rank


def test_top10_decoy_includes_all_top10_plus_decoys_shuffled():
    top10 = [f"Company {i}" for i in range(10)]  # pretend engine top-10
    p = B.build_company_packet(
        pair="x", cohort=B.TOP10_DECOY, roster=_ROSTER,
        run_top10_names=top10, decoy_count=5, seed=3,
    )
    names = set(p.names())
    assert set(top10) <= names                       # all top-10 present
    assert len(p.entries) == 15                       # 10 + 5 decoys
    # decoys are roster entries not in top-10; labeler can't tell which is which
    # (no rank/score field), and order is shuffled (not the input order).
    assert p.names() != top10 + [f"Company {i}" for i in range(10, 15)]


def test_packet_shuffle_is_seed_deterministic():
    a = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=42)
    b = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=42)
    assert a.names() == b.names()
    c = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=43)
    assert c.names() != a.names()  # different seed → different order


# ---------- sealed labels: separate artifact ----------

def test_sealed_labels_is_separate_artifact_with_sha():
    p = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=1)
    sealed = B.seal_company_labels(p, {"Company 0": "target", "Company 1": "competitor"})
    assert isinstance(sealed, B.SealedLabels) and sealed is not p
    assert sealed.labels == {"Company 0": "target", "Company 1": "competitor"}
    assert sealed.sha and sealed.packet_sha
    # packet object is unchanged by sealing (different artifact).
    assert all(set(e.keys()) == {"index", "name"} for e in p.entries)


# ---------- evidence packet: order enforcement (R2-2) ----------

_EVIDENCE = [
    {"company": "Company 0", "credited_type": "official_url",
     "snippet": "...", "url": "https://c0.example", "published_at": None},
    {"company": "Company 1", "credited_type": "news",
     "snippet": "...", "url": "https://news/c1", "published_at": "2026-05-01"},
]


def test_evidence_packet_refuses_before_company_labels_sealed():
    with pytest.raises(ValueError, match="sealed"):
        B.build_evidence_packet(pair="x", top10_evidence=_EVIDENCE, sealed_company_labels=None)


def test_evidence_packet_built_after_seal_hides_engine_output():
    p = B.build_company_packet(pair="x", cohort=B.FULL, roster=_ROSTER, seed=1)
    sealed = B.seal_company_labels(p, {"Company 0": "target"})
    ev = B.build_evidence_packet(pair="x", top10_evidence=_EVIDENCE, sealed_company_labels=sealed)
    assert len(ev.items) == 2
    item = ev.items[0]
    # carries judging context, hides tier/score/rank.
    assert item.company and item.credited_type and item.url
    assert not hasattr(item, "tier") and not hasattr(item, "score")


def test_seal_evidence_verdicts_validates_vocab():
    p = B.EvidencePacket(pair="x", items=[])
    sealed = B.seal_evidence_verdicts(p, ["correct", "wrong-company", "stale"])
    assert sealed.sha and len(sealed.verdicts) == 3
    with pytest.raises(ValueError, match="invalid"):
        B.seal_evidence_verdicts(p, ["correct", "bogus"])
