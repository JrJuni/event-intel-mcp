"""Y1 CS9 — labeling aid: neutral source context + product rubric, no engine
verdict leak; fillable sheet round-trips to a {name: label} map."""
from __future__ import annotations

import pytest

from event_intel.eval import labeling as L

# ---------- context extraction ----------

def test_build_context_first_nonempty_overview_wins():
    records = [
        {"company_title": "A", "pr": "", "intro": "intro text", "zone": "5"},
        {"company_title": "B", "pr": "pr text", "intro": "ignored"},
    ]
    ctx = L.build_context_from_records(
        records, name_key="company_title", overview_keys=("pr", "intro"),
        extra_keys=("zone",),
    )
    assert ctx["A"].overview == "intro text"   # pr empty → falls through to intro
    assert ctx["B"].overview == "pr text"      # pr present → wins
    assert ctx["A"].extra == {"zone": "5"}


def test_build_context_collapses_whitespace_and_truncates():
    long = "x " * 1000
    ctx = L.build_context_from_records(
        [{"n": "C", "d": "  multi   line\n\ttext  "}, {"n": "D", "d": long}],
        name_key="n", overview_keys=("d",),
    )
    assert ctx["C"].overview == "multi line text"
    assert len(ctx["D"].overview) <= L._OVERVIEW_MAX and ctx["D"].overview.endswith("…")


def test_build_context_url_and_missing_overview():
    ctx = L.build_context_from_records(
        [{"n": "E", "u": "https://e.example"}], name_key="n",
        overview_keys=("desc",), url_key="u",
    )
    assert ctx["E"].overview == "" and ctx["E"].url == "https://e.example"


# ---------- product header (rubric, all INPUT not OUTPUT) ----------

_CARD = {
    "product_name": "MongoDB Atlas",
    "one_liner": "AI-ready document database with vector search",
    "capabilities": [{"name": "vector search"}, {"name": "document model"}],
    "ideal_customer": {"industries": ["genAI devs", "fintech"]},
    "competitors": [{"name": "ClickHouse"}, {"name": "Snowflake"}],
    "bad_fit": [{"reason": "pure GPU compute clouds"}],
}


def test_product_header_includes_rubric_pieces():
    h = L.product_header_from_card(_CARD, lang="ko")
    assert "MongoDB Atlas" in h and "vector search" in h
    assert "genAI devs" in h and "ClickHouse" in h  # competitors shown as reference
    assert "pure GPU compute clouds" in h


# ---------- sheet build + worksheet render: no engine-verdict leak ----------

_PACKET_ENTRIES = [
    {"index": 0, "name": "ClickHouse"},
    {"index": 1, "name": "CoreWeave"},
]


def _ctx():
    return L.build_context_from_records(
        [{"n": "ClickHouse", "d": "OLAP column store", "u": "https://clickhouse.com"},
         {"n": "CoreWeave", "d": "GPU cloud"}],
        name_key="n", overview_keys=("d",), url_key="u",
    )


def test_sheet_rows_carry_context_and_blank_label_only():
    sheet = L.build_labeling_sheet(_PACKET_ENTRIES, _ctx())
    assert {r["name"] for r in sheet} == {"ClickHouse", "CoreWeave"}
    for r in sheet:
        assert set(r) == {"index", "name", "overview", "url", "label"}
        assert r["label"] == ""                       # blank to fill
        # the only engine-derived thing (packet) was names; no score/tier/rank
        assert "tier" not in r and "score" not in r and "rank" not in r
    assert dict((r["name"], r["overview"]) for r in sheet)["ClickHouse"] == "OLAP column store"


def test_worksheet_md_has_rubric_and_every_company_no_verdict():
    # third entry has no source context → exercises the missing-overview notice
    entries = [*_PACKET_ENTRIES, {"index": 2, "name": "NoContextCo"}]
    sheet = L.build_labeling_sheet(entries, _ctx())
    md = L.render_worksheet_md(
        pair="p1", product_header=L.product_header_from_card(_CARD), sheet=sheet,
    )
    assert "ClickHouse" in md and "CoreWeave" in md and "NoContextCo" in md
    assert "OLAP column store" in md          # neutral overview present
    assert "target` / `competitor" in md     # label vocab present
    # missing overview gets a notice, not a crash
    assert "(소스에 설명 없음)" in md


# ---------- parse filled sheet ----------

def test_parse_filled_sheet_maps_names_to_labels():
    sheet = [
        {"index": 0, "name": "ClickHouse", "label": "competitor"},
        {"index": 1, "name": "CoreWeave", "label": "target"},
    ]
    assert L.parse_filled_sheet(sheet) == {"ClickHouse": "competitor", "CoreWeave": "target"}


def test_parse_rejects_invalid_label():
    with pytest.raises(ValueError, match="invalid"):
        L.parse_filled_sheet([{"name": "X", "label": "rival"}])


def test_parse_requires_all_by_default_but_allows_partial():
    sheet = [{"name": "A", "label": "target"}, {"name": "B", "label": ""}]
    with pytest.raises(ValueError, match="unlabeled"):
        L.parse_filled_sheet(sheet)
    assert L.parse_filled_sheet(sheet, require_all=False) == {"A": "target"}
