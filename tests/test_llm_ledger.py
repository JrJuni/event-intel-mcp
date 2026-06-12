"""Y1D D0 — LLM usage ledger + reference-model cost conversion + cost CLI eval.

Adversarial set: malformed usage values, malformed pricing entries, zero/negative
tokens, model-less providers, concurrent recording, pre-D0 run summaries in the
cost aggregator, and the no-ledger (None) seams staying behavior-identical.
"""
from __future__ import annotations

import json
import threading

from event_intel.eval import cost as cost_eval
from event_intel.events.enrichment import EnrichedExhibitor
from event_intel.events.run_summary import RunSummary
from event_intel.rag.retriever import FitResult
from event_intel.runtime.llm_ledger import LlmUsageLedger
from event_intel.scoring.compute import score_exhibitors

PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.50},
}


# ---------- ledger accumulation ----------


def test_ledger_accumulates_per_stage_and_totals():
    led = LlmUsageLedger()
    led.record("extraction", "m1", {"input_tokens": 100, "output_tokens": 10})
    led.record("extraction", "m1", {"input_tokens": 200, "output_tokens": 20})
    led.record("rationale", "m2", {"input_tokens": 50, "output_tokens": 5})
    s = led.summary()
    assert s["stages"]["extraction"] == {
        "calls": 2, "calls_cached": 0, "input_tokens": 300, "output_tokens": 30,
        "models": ["m1"],
    }
    assert s["stages"]["rationale"]["calls"] == 1
    assert s["totals"] == {
        "calls": 3, "calls_cached": 0, "input_tokens": 350, "output_tokens": 35,
    }


def test_ledger_tolerates_malformed_usage():
    led = LlmUsageLedger()
    led.record("x", "m", None)
    led.record("x", "m", {})
    led.record("x", "m", {"input_tokens": "abc", "output_tokens": None})
    led.record("x", "m", {"input_tokens": -50, "output_tokens": 3.7})
    s = led.summary()
    # strings/None/negatives → 0; floats truncate via int()
    assert s["stages"]["x"]["input_tokens"] == 0
    assert s["stages"]["x"]["output_tokens"] == 3
    assert s["stages"]["x"]["calls"] == 4


def test_ledger_preaggregated_calls_and_empty_model():
    led = LlmUsageLedger()
    led.record("extraction", "", {"input_tokens": 30_000, "output_tokens": 12_000},
               calls=12)
    led.record("extraction", "m", {}, calls=0)
    s = led.summary()
    assert s["stages"]["extraction"]["calls"] == 12
    # empty model string is not collected; real one is
    assert s["stages"]["extraction"]["models"] == ["m"]


def test_ledger_record_cached_separate_from_calls_and_tokens():
    # #16-⑤: cached calls count in calls_cached only — calls/tokens/cost untouched.
    led = LlmUsageLedger()
    led.record("extraction", "m", {"input_tokens": 100, "output_tokens": 10}, calls=2)
    led.record_cached("extraction", calls=5)
    led.record_cached("extraction")  # default calls=1
    s = led.summary(PRICING)
    e = s["stages"]["extraction"]
    assert e["calls"] == 2
    assert e["calls_cached"] == 6
    assert e["input_tokens"] == 100 and e["output_tokens"] == 10
    assert s["totals"]["calls_cached"] == 6
    expected_usd = round(100 / 1e6 * 3.00 + 10 / 1e6 * 15.00, 6)
    assert s["reference_costs_usd"]["claude-sonnet-4-6"]["total_usd"] == expected_usd


def test_ledger_cached_only_stage_is_costless_not_unpriced():
    led = LlmUsageLedger()
    led.record_cached("extraction", calls=12)
    s = led.summary(PRICING)
    assert s["stages"]["extraction"] == {
        "calls": 0, "calls_cached": 12, "input_tokens": 0, "output_tokens": 0,
        "models": [],
    }
    # zero-token stage: skipped from blended pricing, NOT flagged unpriced
    assert s["blended_cost_usd"]["stages"] == {}
    assert s["blended_cost_usd"]["unpriced_stages"] == []


def test_ledger_record_cached_negative_clamped():
    led = LlmUsageLedger()
    led.record_cached("x", calls=-3)
    assert led.summary()["stages"]["x"]["calls_cached"] == 0


def test_ledger_empty_summary_is_graceful():
    s = LlmUsageLedger().summary(PRICING)
    assert s["totals"] == {
        "calls": 0, "calls_cached": 0, "input_tokens": 0, "output_tokens": 0,
    }
    assert s["reference_costs_usd"]["claude-sonnet-4-6"]["total_usd"] == 0.0
    assert "pricing_warnings" not in s


def test_ledger_concurrent_recording_is_exact():
    led = LlmUsageLedger()

    def worker():
        for _ in range(200):
            led.record("stage", "m", {"input_tokens": 1, "output_tokens": 2})

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = led.summary()
    assert s["totals"] == {
        "calls": 1600, "calls_cached": 0, "input_tokens": 1600, "output_tokens": 3200,
    }


# ---------- reference cost conversion ----------


def test_cost_conversion_exact_arithmetic():
    led = LlmUsageLedger()
    led.record("extraction", "any", {"input_tokens": 1_000_000, "output_tokens": 200_000})
    costs = led.summary(PRICING)["reference_costs_usd"]
    assert costs["claude-sonnet-4-6"] == {
        "input_usd": 3.0, "output_usd": 3.0, "total_usd": 6.0,
    }
    assert costs["gpt-5.4-mini"] == {
        "input_usd": 0.75, "output_usd": 0.9, "total_usd": 1.65,
    }


def test_cost_conversion_skips_malformed_pricing_with_warning():
    led = LlmUsageLedger()
    led.record("x", "m", {"input_tokens": 100, "output_tokens": 100})
    s = led.summary({
        "good": {"input": 1.0, "output": 1.0},
        "no-output": {"input": 1.0},
        "non-numeric": {"input": "three", "output": 15},
    })
    assert set(s["reference_costs_usd"]) == {"good"}
    assert len(s["pricing_warnings"]) == 2


def test_cost_conversion_none_pricing():
    s = LlmUsageLedger().summary(None)
    assert s["reference_costs_usd"] == {}


# ---------- blended cost (schema v2, #16-④ right-sizing) ----------

BLEND_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}


def test_blended_cost_prices_each_stage_by_its_recorded_model():
    led = LlmUsageLedger()
    led.record("extraction", "claude-sonnet-4-6",
               {"input_tokens": 1_000_000, "output_tokens": 200_000})
    led.record("triage", "claude-haiku-4-5",
               {"input_tokens": 1_000_000, "output_tokens": 200_000})
    s = led.summary(BLEND_PRICING)
    blended = s["blended_cost_usd"]
    assert s["schema"] == "llm-usage/v2"
    # sonnet: 1M*3 + 0.2M*15 = 6.0 ; haiku: 1M*1 + 0.2M*5 = 2.0
    assert blended["stages"] == {"extraction": 6.0, "triage": 2.0}
    assert blended["total_usd"] == 8.0
    assert blended["unpriced_stages"] == []
    # reference conversion (all-on-one-model) is unchanged by blending
    assert s["reference_costs_usd"]["claude-sonnet-4-6"]["total_usd"] == 12.0


def test_blended_cost_unknown_model_lands_in_unpriced():
    led = LlmUsageLedger()
    led.record("extraction", "gpt-5.5", {"input_tokens": 100, "output_tokens": 10})
    led.record("rationale", "claude-haiku-4-5",
               {"input_tokens": 1_000_000, "output_tokens": 0})
    blended = led.summary(BLEND_PRICING)["blended_cost_usd"]
    assert blended["unpriced_stages"] == ["extraction"]
    assert blended["stages"] == {"rationale": 1.0}
    assert blended["total_usd"] == 1.0


def test_blended_cost_mixed_model_stage_is_unpriced_not_mispriced():
    led = LlmUsageLedger()
    led.record("llm_fit", "claude-haiku-4-5", {"input_tokens": 100, "output_tokens": 10})
    led.record("llm_fit", "claude-sonnet-4-6", {"input_tokens": 100, "output_tokens": 10})
    blended = led.summary(BLEND_PRICING)["blended_cost_usd"]
    assert blended["unpriced_stages"] == ["llm_fit"]
    assert blended["stages"] == {}


def test_blended_cost_zero_token_and_empty_stages_skipped():
    led = LlmUsageLedger()
    led.record("extraction", "claude-sonnet-4-6",
               {"input_tokens": 0, "output_tokens": 0}, calls=0)  # CSV short-circuit shape
    s = led.summary(BLEND_PRICING)
    assert s["blended_cost_usd"] == {
        "stages": {}, "total_usd": 0.0, "unpriced_stages": [],
    }
    empty = LlmUsageLedger().summary(BLEND_PRICING)
    assert empty["blended_cost_usd"]["total_usd"] == 0.0


# ---------- run_summary carries the block; ledger seams default to None ----------


def _minimal_run_summary(**overrides) -> RunSummary:
    base = dict(
        run_id="r1", run_fingerprint="f", git_commit_sha="g", config_fp="c",
        cards_fingerprint=None, source_sha256=None, provider="anthropic",
        model_ids={}, reference_timestamp="t", target_mode="customer",
        max_companies=None, max_chunks_per_event=None, refresh=False,
        cache_hits=0, cache_misses=0, skipped_from_resume=0, search_calls=0,
        extracted=0, enriched=0, scored=0, extraction_coverage=None,
    )
    base.update(overrides)
    return RunSummary(**base)


def test_run_summary_llm_usage_field_default_none():
    rs = _minimal_run_summary()
    assert rs.to_dict()["llm_usage"] is None
    rs2 = _minimal_run_summary(llm_usage={"totals": {"calls": 1}})
    assert rs2.to_dict()["llm_usage"]["totals"]["calls"] == 1


class _FakeResp:
    def __init__(self):
        self.text = "RATIONALE: fits well. ANGLE: pitch X."
        self.usage = {"input_tokens": 40, "output_tokens": 8}


class _FakeLLM:
    model = "fake-model"

    def chat_once(self, **_kw):
        return _FakeResp()


class _ModellessLLM:
    # no .model attribute — record must getattr-fallback, not raise
    def chat_once(self, **_kw):
        return _FakeResp()


def _score_kwargs(llm):
    row = EnrichedExhibitor(name="Acme", source_snippet="builds rockets")
    fit = FitResult(name="Acme", capability_fit=0.9, top_hits=[])
    cfg = {
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
        }
    }
    return dict(
        enriched=[row], fit_results=[fit], cards=None, config=cfg, top_k=5,
        llm_provider=llm, rationale_for_tiers=("S", "A", "B", "C"),
    )


def test_rationale_records_into_ledger():
    led = LlmUsageLedger()
    summary = score_exhibitors(**_score_kwargs(_FakeLLM()), usage_ledger=led)
    assert summary.rationale_calls == 1
    s = led.summary()
    assert s["stages"]["rationale"]["input_tokens"] == 40
    assert s["stages"]["rationale"]["models"] == ["fake-model"]


def test_rationale_without_ledger_unchanged():
    summary = score_exhibitors(**_score_kwargs(_FakeLLM()))
    assert summary.rationale_calls == 1


def test_rationale_modelless_provider_does_not_raise():
    led = LlmUsageLedger()
    score_exhibitors(**_score_kwargs(_ModellessLLM()), usage_ledger=led)
    assert led.summary()["stages"]["rationale"]["models"] == []


# ---------- eval/cost.py aggregation ----------


def _write_summary(path, *, run_id, llm_usage):
    payload = {"run_id": run_id, "pair": "p1", "provider": "anthropic"}
    if llm_usage is not None:
        payload["llm_usage"] = llm_usage
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _usage_block(tokens_in, tokens_out, cost):
    return {
        "stages": {"extraction": {
            "calls": 1, "input_tokens": tokens_in, "output_tokens": tokens_out,
        }},
        "totals": {"calls": 1, "input_tokens": tokens_in, "output_tokens": tokens_out},
        "reference_costs_usd": {"claude-sonnet-4-6": {"total_usd": cost}},
    }


def test_cost_aggregate_sums_runs_and_flags_pre_d0(tmp_path):
    _write_summary(tmp_path / "a" / "run_summary.json", run_id="r1",
                   llm_usage=_usage_block(100, 10, 0.5))
    _write_summary(tmp_path / "b" / "run_summary.json", run_id="r2",
                   llm_usage=_usage_block(200, 20, 1.25))
    _write_summary(tmp_path / "c" / "run_summary.json", run_id="old", llm_usage=None)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "run_summary.json").write_text("{broken", encoding="utf-8")

    paths = cost_eval.collect_run_summaries(tmp_path)
    assert len(paths) == 4
    agg = cost_eval.aggregate_costs(paths)
    assert len(agg["runs"]) == 2
    assert agg["totals"]["input_tokens"] == 300
    assert agg["totals"]["reference_costs_usd"]["claude-sonnet-4-6"] == 1.75
    assert agg["runs_without_usage"] == 1
    assert agg["skipped_unreadable"] == 1


def test_cost_collect_single_file_and_missing_dir(tmp_path):
    f = tmp_path / "run_summary.json"
    _write_summary(f, run_id="r1", llm_usage=_usage_block(1, 1, 0.0))
    assert cost_eval.collect_run_summaries(f) == [f]
    assert cost_eval.collect_run_summaries(tmp_path / "nope") == []
    other = tmp_path / "other.json"
    other.write_text("{}", encoding="utf-8")
    assert cost_eval.collect_run_summaries(other) == []


def test_cost_render_table_has_totals_and_pre_d0_note(tmp_path):
    _write_summary(tmp_path / "a" / "run_summary.json", run_id="r1",
                   llm_usage=_usage_block(1_000, 100, 0.0045))
    _write_summary(tmp_path / "b" / "run_summary.json", run_id="old", llm_usage=None)
    agg = cost_eval.aggregate_costs(cost_eval.collect_run_summaries(tmp_path))
    table = cost_eval.render_cost_table(agg)
    assert "r1" in table and "claude-sonnet-4-6" in table
    assert "**total**" in table
    assert "1 run(s) had no llm_usage block" in table
