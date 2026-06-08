"""Y1 CS2 — roster normalization + cardinality matching + coverage."""
from __future__ import annotations

from event_intel.eval import roster as R

# ---------- normalization ----------

def test_match_norm_strips_legal_and_punct_and_case():
    assert R.match_norm("ACME Robotics, Inc.") == "acme robotics"
    assert R.match_norm("（株）モビウス") == R.match_norm("モビウス")
    assert R.match_norm("주식회사 효돌") == "효돌"


# ---------- roster build (HCR-shaped records) ----------

def test_build_roster_assigns_ids_and_aliases():
    records = [
        {"company_title": "会社A", "company_title_en": "Company A"},
        {"company_title": "会社B", "company_title_en": "Company B"},
    ]
    roster = R.build_roster_from_records(
        records, id_prefix="hcr", name_keys=("company_title", "company_title_en"),
    )
    assert [e.roster_id for e in roster] == ["hcr-001", "hcr-002"]
    assert roster[0].canonical_name == "会社A"
    assert "Company A" in roster[0].aliases


def test_build_roster_uses_explicit_id_key_and_label():
    records = [{"id": "x9", "name": "ACME", "kind": "competitor"}]
    roster = R.build_roster_from_records(
        records, id_prefix="e", name_keys=("name",), label_key="kind", id_key="id",
    )
    assert roster[0].roster_id == "x9" and roster[0].label == "competitor"


# ---------- matching cardinality ----------

_ROSTER = [
    R.RosterEntry("r1", "ACME Robotics", ["ACME Robotics Inc."]),
    R.RosterEntry("r2", "Globex"),
    R.RosterEntry("r3", "Initech"),
]


def test_exact_and_alias_match():
    m = R.match_roster(["ACME Robotics Inc.", "Globex"], _ROSTER)
    assert m.matched == {"r1": "ACME Robotics Inc.", "r2": "Globex"}
    assert m.method["r1"] == "exact"


def test_levenshtein_match():
    m = R.match_roster(["Initecho"], _ROSTER, threshold=0.8)  # ~Initech
    assert m.matched.get("r3") == "Initecho"
    assert m.method["r3"] == "levenshtein"


def test_n_to_1_merge_dedupes():
    """Two extracted rows for the same roster entry → one materialized, one dup."""
    m = R.match_roster(["Globex", "GLOBEX"], _ROSTER)
    assert m.matched["r2"] in ("Globex", "GLOBEX")
    assert len(m.duplicates) == 1 and m.duplicates[0][1] == "r2"


def test_unmatched_extracted():
    m = R.match_roster(["Totally Unrelated Co"], _ROSTER)
    assert m.unmatched_extracted == ["Totally Unrelated Co"]
    assert not m.matched


def test_ambiguous_one_name_two_roster():
    roster = [R.RosterEntry("a", "Delta"), R.RosterEntry("b", "Delta")]  # same norm
    m = R.match_roster(["Delta"], roster)
    assert m.ambiguous == ["Delta"] and not m.matched


def test_1_to_n_split_via_manual_resolution_materializes_only_primary():
    """Parent booth names 3 roster companies; engine made 1 scoring row →
    primary materialized, the other two mention-only (R3-4)."""
    roster = [
        R.RosterEntry("p1", "BigGroup Robotics"),
        R.RosterEntry("p2", "BigGroup Care"),
        R.RosterEntry("p3", "BigGroup Mobility"),
    ]
    m = R.match_roster(
        ["BigGroup (booth)"], roster,
        manual_resolutions={"BigGroup (booth)": {"primary": "p1", "mentions": ["p2", "p3"]}},
    )
    assert m.matched == {"p1": "BigGroup (booth)"} and m.method["p1"] == "manual"
    assert m.mention_only == {"p2", "p3"}
    # extraction_coverage counts ONLY the materialized entity (1/3), mention 3/3.
    assert R.coverage(m, roster).value == 1 / 3
    assert R.mention_coverage(m, roster).value == 1.0


# ---------- coverage + join projection ----------

def test_coverage_materialized_only():
    m = R.match_roster(["ACME Robotics", "Globex"], _ROSTER)
    assert R.coverage(m, _ROSTER).value == 2 / 3


def test_tiers_by_roster_id_projection():
    m = R.match_roster(["ACME Robotics", "Globex"], _ROSTER)
    tiers = R.tiers_by_roster_id(m, {"ACME Robotics": "S", "Globex": "B"})
    assert tiers == {"r1": "S", "r2": "B"}


def test_labels_by_roster_id():
    roster = [R.RosterEntry("r1", "A", label="competitor"), R.RosterEntry("r2", "B")]
    assert R.labels_by_roster_id(roster) == {"r1": "competitor"}
