"""Host critique protocol tests — BD critique harness S3.

Verifies the panel prompt + brief renderer, that the prompt's JSON contract
matches the S2 schema keys, and a CLI smoke of `benchmark critique-brief`.
"""
from __future__ import annotations

import json

import yaml
from typer.testing import CliRunner

from event_intel.cli import app
from event_intel.eval.critique_packet import EXPECTED_LENSES, build_critique_packet
from event_intel.eval.critique_panel import load_panel_prompt, render_critique_brief

runner = CliRunner()

_PACKET = {
    "schema_version": 1,
    "pair": "p1",
    "product_header": "**Product**: Acme DB",
    "lenses": list(EXPECTED_LENSES),
    "picks": [
        {"name": "Acme", "tier": "S", "final_score": 8.1, "capability_fit": 0.7,
         "rationale": "fits", "evidence": [{"type": "news", "url": "https://n/1"}]},
    ],
    "packet_sha": "deadbeef",
}


# ---------- prompt loader + contract ----------


def test_panel_prompt_loads_en_and_ko():
    for lang in ("en", "ko"):
        assert "would_place_sa" in load_panel_prompt(lang)


def test_panel_prompt_falls_back_to_en():
    assert load_panel_prompt("xx") == load_panel_prompt("en")


def test_prompt_contract_matches_s2_schema_keys():
    """The host JSON contract in the prompt must name every key parse_critique
    requires — otherwise the host produces JSON the validator rejects."""
    p = load_panel_prompt("en")
    for token in ("packet_sha", "independent_first", "would_place_sa",
                  "defensible", "flag", "judge_model_id", *EXPECTED_LENSES):
        assert token in p, token


# ---------- render_critique_brief ----------


def test_brief_includes_prompt_picks_and_echo_fields():
    brief = render_critique_brief(_PACKET, lang="en")
    assert "would_place_sa" in brief  # panel prompt embedded
    assert "packet_sha: deadbeef" in brief and "pair: p1" in brief
    assert "Acme" in brief and "news:https://n/1" in brief
    assert "**Product**: Acme DB" in brief
    assert "S/A PICKS (1)" in brief


def test_brief_handles_empty_picks():
    pkt = dict(_PACKET, picks=[])
    brief = render_critique_brief(pkt, lang="en")
    assert "S/A PICKS (0)" in brief


# ---------- CLI smoke ----------


def test_cli_critique_brief_smoke(tmp_path):
    tl = tmp_path / "tier_list.yaml"
    tl.write_text(yaml.safe_dump({"exhibitors": [
        {"name": "Acme", "tier": "S", "final_score": 8.0, "capability_fit": 0.6,
         "rationale": "fits", "evidence": [{"type": "news", "url": "https://n/1"}]},
        {"name": "Zeta", "tier": "C", "final_score": 1.0},
    ]}), encoding="utf-8")
    card = tmp_path / "card.yaml"
    card.write_text(yaml.safe_dump({
        "product_name": "Acme DB", "one_liner": "a database",
        "capabilities": [{"name": "scale"}], "competitors": [], "bad_fit": [],
    }), encoding="utf-8")
    out_packet = tmp_path / "packet.json"
    out_brief = tmp_path / "brief.md"

    res = runner.invoke(app, [
        "benchmark", "critique-brief", "--tier-list", str(tl), "--card", str(card),
        "--pair", "p1", "--out-packet", str(out_packet), "--out-brief", str(out_brief),
    ])
    assert res.exit_code == 0, res.output
    packet = json.loads(out_packet.read_text(encoding="utf-8"))
    assert [p["name"] for p in packet["picks"]] == ["Acme"]  # only S/A
    # brief embeds the packet_sha so the host echoes it back
    assert packet["packet_sha"] in out_brief.read_text(encoding="utf-8")


def test_cli_critique_brief_top_n_pulls_non_sa_pick(tmp_path):
    tl = tmp_path / "tier_list.yaml"
    tl.write_text(yaml.safe_dump({"exhibitors": [
        {"name": "Acme", "tier": "S", "final_score": 8.0},
        {"name": "Ramp", "tier": "B", "final_score": 6.0},  # high-scoring non-S/A
        {"name": "Zeta", "tier": "C", "final_score": 1.0},
    ]}), encoding="utf-8")
    card = tmp_path / "card.yaml"
    card.write_text(yaml.safe_dump({"product_name": "P", "one_liner": "x"}), encoding="utf-8")
    out_packet = tmp_path / "packet.json"
    out_brief = tmp_path / "brief.md"

    res = runner.invoke(app, [
        "benchmark", "critique-brief", "--tier-list", str(tl), "--card", str(card),
        "--pair", "p1", "--top-n", "2",
        "--out-packet", str(out_packet), "--out-brief", str(out_brief),
    ])
    assert res.exit_code == 0, res.output
    packet = json.loads(out_packet.read_text(encoding="utf-8"))
    names = [p["name"] for p in packet["picks"]]
    assert names == ["Acme", "Ramp"]  # S + top-2 by score; Zeta excluded
    ramp = next(p for p in packet["picks"] if p["name"] == "Ramp")
    assert ramp["selected_for"] == ["top_score"]


def test_cli_brief_then_parse_round_trip(tmp_path):
    """A critique that echoes the CLI-built packet_sha validates against S2."""
    from event_intel.eval.critique_packet import parse_critique

    pkt = build_critique_packet(
        pair="p1", tier_list={"exhibitors": [
            {"name": "Acme", "tier": "A", "final_score": 6.0}]},
        product_header="H",
    )
    critique = {
        "pair": "p1", "packet_sha": pkt["packet_sha"], "judge_model_id": "host:claude",
        "picks": [{
            "name": "Acme",
            "independent_first": {"would_place_sa": False, "reason": "thin"},
            "lenses": {lk: {"verdict": "disagree", "reason": "r"} for lk in EXPECTED_LENSES},
            "defensible": False, "flag": True,
        }],
    }
    out = parse_critique(critique, expected_packet_sha=pkt["packet_sha"])
    assert out["picks"][0]["flag"] is True
