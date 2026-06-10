"""Critique packet + schema tests — BD critique harness S2."""
from __future__ import annotations

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.eval.critique_packet import (
    EXPECTED_LENSES,
    build_critique_packet,
    parse_critique,
)

_TIER_LIST = {
    "exhibitors": [
        {"name": "Acme", "tier": "S", "final_score": 8.1, "capability_fit": 0.7,
         "rationale": "fits", "evidence": [{"type": "news", "url": "https://n/1"}]},
        {"name": "Beta", "tier": "A", "final_score": 6.4, "capability_fit": 0.5,
         "rationale": "ok", "evidence": []},
        {"name": "Gamma", "tier": "B", "final_score": 4.0},
        {"name": "Delta", "tier": "C", "final_score": 1.0},
    ]
}


# ---------- build_critique_packet ----------


def test_packet_includes_only_sa_picks():
    pkt = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="H")
    names = [p["name"] for p in pkt["picks"]]
    assert names == ["Acme", "Beta"]  # B/C dropped


def test_packet_pick_fields_and_header():
    pkt = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="PROD")
    acme = pkt["picks"][0]
    assert acme["tier"] == "S" and acme["final_score"] == 8.1
    assert acme["capability_fit"] == 0.7 and acme["rationale"] == "fits"
    assert acme["evidence"] == [{"type": "news", "url": "https://n/1"}]
    assert pkt["product_header"] == "PROD"
    assert pkt["lenses"] == list(EXPECTED_LENSES)


def test_packet_sha_is_deterministic_and_present():
    a = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="H")
    b = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="H")
    assert a["packet_sha"] and a["packet_sha"] == b["packet_sha"]
    c = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="DIFFERENT")
    assert c["packet_sha"] != a["packet_sha"]


def test_empty_tier_list_yields_no_picks():
    pkt = build_critique_packet(pair="p1", tier_list={"exhibitors": []}, product_header="H")
    assert pkt["picks"] == []


# ---------- parse_critique ----------


def _good_critique(packet_sha="abc"):
    lens = {"verdict": "agree", "reason": "r"}
    return {
        "pair": "p1",
        "packet_sha": packet_sha,
        "judge_model_id": "host:claude",
        "picks": [
            {
                "name": "Acme",
                "independent_first": {"would_place_sa": True, "reason": "I'd S it too"},
                "lenses": {lk: dict(lens) for lk in EXPECTED_LENSES},
                "defensible": True,
                "flag": False,
            }
        ],
    }


def test_valid_critique_passes():
    out = parse_critique(_good_critique())
    assert out["picks"][0]["name"] == "Acme"


def test_packet_sha_mismatch_raises():
    with pytest.raises(MCPError) as exc:
        parse_critique(_good_critique("xxx"), expected_packet_sha="yyy")
    assert exc.value.error_code == ErrorCode.SCHEMA_ERROR
    assert "packet_sha" in exc.value.message


def test_packet_sha_match_ok():
    c = _good_critique("sha123")
    assert parse_critique(c, expected_packet_sha="sha123")["packet_sha"] == "sha123"


@pytest.mark.parametrize("key", ["pair", "packet_sha", "judge_model_id", "picks"])
def test_missing_top_level_key_raises(key):
    c = _good_critique()
    del c[key]
    with pytest.raises(MCPError):
        parse_critique(c)


def test_would_place_sa_must_be_bool():
    c = _good_critique()
    c["picks"][0]["independent_first"]["would_place_sa"] = "yes"
    with pytest.raises(MCPError) as exc:
        parse_critique(c)
    assert "would_place_sa" in exc.value.message


def test_missing_lens_raises():
    c = _good_critique()
    del c["picks"][0]["lenses"]["competitor"]
    with pytest.raises(MCPError) as exc:
        parse_critique(c)
    assert "lens" in exc.value.message.lower()


def test_bad_verdict_raises():
    c = _good_critique()
    c["picks"][0]["lenses"]["customer_fit"]["verdict"] = "maybe"
    with pytest.raises(MCPError):
        parse_critique(c)


def test_missing_lens_reason_raises():
    c = _good_critique()
    c["picks"][0]["lenses"]["customer_fit"]["reason"] = "  "
    with pytest.raises(MCPError):
        parse_critique(c)


@pytest.mark.parametrize("field", ["defensible", "flag"])
def test_defensible_flag_must_be_bool(field):
    c = _good_critique()
    c["picks"][0][field] = 1
    with pytest.raises(MCPError):
        parse_critique(c)


# ---------- round trip ----------


def test_packet_to_critique_round_trip():
    pkt = build_critique_packet(pair="p1", tier_list=_TIER_LIST, product_header="H")
    critique = _good_critique(pkt["packet_sha"])
    out = parse_critique(critique, expected_packet_sha=pkt["packet_sha"])
    assert out["packet_sha"] == pkt["packet_sha"]
