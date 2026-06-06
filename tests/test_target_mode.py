"""Phase 18V item 2 — target_mode resolution, card-load contract, mode factors."""
from __future__ import annotations

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import FitResult
from event_intel.scoring.compute import score_exhibitors
from event_intel.tools import build_event_tier_list as bet


class _Cards:
    def __init__(self, target_mode):
        self.target_mode = target_mode


# ---------- resolve precedence (None sentinel) ----------


def test_resolve_target_mode_precedence():
    cards = _Cards("ecosystem")
    cfg = {"target_mode": "partner"}
    # explicit arg wins over everything
    assert bet._resolve_target_mode("customer", cfg, cards) == "customer"
    # no arg → user config wins over card
    assert bet._resolve_target_mode(None, cfg, cards) == "partner"
    # no arg, no config → card default
    assert bet._resolve_target_mode(None, {}, cards) == "ecosystem"
    # nothing anywhere → customer
    assert bet._resolve_target_mode(None, {}, None) == "customer"


def test_resolve_target_mode_rejects_invalid():
    with pytest.raises(MCPError) as exc:
        bet._resolve_target_mode("frenemy", {}, None)
    assert exc.value.error_code == ErrorCode.INVALID_INPUT


# ---------- card-load contract (absent vs invalid) ----------


def test_card_absent_returns_warning_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(bet, "_outputs_base", lambda: tmp_path)
    (tmp_path / "ws1").mkdir()
    cards, warning = bet._load_cards_or_warn("ws1")
    assert cards is None
    assert warning and "no capability_cards" in warning


def test_card_present_but_invalid_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bet, "_outputs_base", lambda: tmp_path)
    ws = tmp_path / "ws2"
    ws.mkdir()
    (ws / "capability_cards.yaml").write_text("not: [valid, cards", encoding="utf-8")
    with pytest.raises(MCPError):
        bet._load_cards_or_warn("ws2")


# ---------- mode factor effect on scoring ----------


def _cfg_with_modes():
    return {
        "scoring": {
            "weights": {
                "capability_fit": 0.30, "source_confidence": 0.15,
                "buying_signal": 0.15, "website_verification": 0.10,
                "category_fit": 0.15, "competitor_penalty": -0.35,
                "bad_fit_penalty": -0.25,
            },
            "tier_rules": {
                "S": {"min_final_score": 7.5, "evidence_floor_min": 2},
                "A": {"min_final_score": 6.0, "evidence_floor_min": 1},
                "B": {"min_final_score": 4.0, "evidence_floor_min": 0},
                "C": {"min_final_score": 0.0, "evidence_floor_min": 0},
            },
            "retrieval": {"negative_sim_threshold": 0.5},
            "target_mode": {
                "customer": {"competitor_penalty_factor": 1.0, "bad_fit_penalty_factor": 1.0},
                "partner": {"competitor_penalty_factor": 0.0, "bad_fit_penalty_factor": 1.0},
                "ecosystem": {"competitor_penalty_factor": 0.0, "bad_fit_penalty_factor": 0.0},
            },
        }
    }


def test_partner_mode_neutralizes_competitor_penalty():
    row = EnrichedExhibitor(
        name="Rival", source_snippet="x", official_url="https://rival.example",
        news_signals=[NewsSignal("Rival news", "u", "s")],
    )
    fit = FitResult(name="Rival", capability_fit=0.95, top_hits=[], competitor_similarity=0.9)
    cfg = _cfg_with_modes()
    customer = score_exhibitors(
        enriched=[row], fit_results=[fit], cards=None, config=cfg, top_k=5,
        target_mode="customer",
    ).rows[0]
    partner = score_exhibitors(
        enriched=[row], fit_results=[fit], cards=None, config=cfg, top_k=5,
        target_mode="partner",
    ).rows[0]
    # Under partner the competitor penalty is zeroed → strictly higher score and
    # at least as high a tier (a competitor reaches the tier its score earns).
    assert partner.final_score > customer.final_score
    order = ["C", "B", "A", "S"]
    assert order.index(partner.tier) >= order.index(customer.tier)
