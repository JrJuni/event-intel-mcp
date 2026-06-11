"""Y1D D0 — per-build LLM usage ledger + reference-model cost conversion.

Every LLM call site in a build (extraction / rescue / rationale / llm_fit /
triage) records its token usage here; the build folds ``summary()`` into
``run_summary.llm_usage`` so each run carries its own measured cost, converted
against the REFERENCE models (config ``llm.reference_pricing``) regardless of
the provider actually used — a free ChatGPT-OAuth run still shows "what this
would have cost on claude-sonnet-4-6 / gpt-5.4-mini".

Pricing verified 2026-06-11 (official pages): claude-sonnet-4-6 $3/$15 per
1M tokens; gpt-5.4-mini $0.75/$4.50 (gpt-5.5-mini does not exist — 5.4-mini is
the current mini tier). Rates live in config/defaults.yaml, not here.

Pure stdlib — import-cold safe (guarded by tests/test_mcp_cold_start.py).
"""
from __future__ import annotations

import threading

LLM_USAGE_SCHEMA = "llm-usage/v1"

_TOKEN_KEYS = ("input_tokens", "output_tokens")


class LlmUsageLedger:
    """Accumulate LLM token usage per pipeline stage for ONE build.

    Thread-safe (a build is single-threaded today, but recording is cheap and
    callers shouldn't have to care). ``usage`` dicts are the provider
    ``LLMResponse.usage`` shape — missing / non-numeric token values count as 0
    rather than raising, so a provider quirk can never fail a build.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stages: dict[str, dict] = {}

    @staticmethod
    def _tok(usage: dict | None, key: str) -> int:
        try:
            return max(0, int((usage or {}).get(key) or 0))
        except (TypeError, ValueError):
            return 0

    def record(
        self, stage: str, model: str, usage: dict | None, *, calls: int = 1
    ) -> None:
        """Add one (or, for pre-aggregated stages, ``calls``) LLM call's usage."""
        with self._lock:
            entry = self._stages.setdefault(
                stage,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "models": set()},
            )
            entry["calls"] += max(0, int(calls))
            for key in _TOKEN_KEYS:
                entry[key] += self._tok(usage, key)
            if model:
                entry["models"].add(str(model))

    def summary(self, reference_pricing: dict | None = None) -> dict:
        """Snapshot: per-stage usage + totals + reference-model USD conversion.

        ``reference_pricing`` is ``{model_key: {input: $/1M, output: $/1M}}``.
        Malformed entries are skipped with a warning instead of raising — cost
        reporting is auxiliary and must never fail a build.
        """
        with self._lock:
            stages = {
                stage: {
                    "calls": e["calls"],
                    "input_tokens": e["input_tokens"],
                    "output_tokens": e["output_tokens"],
                    "models": sorted(e["models"]),
                }
                for stage, e in sorted(self._stages.items())
            }
        totals = {
            "calls": sum(e["calls"] for e in stages.values()),
            "input_tokens": sum(e["input_tokens"] for e in stages.values()),
            "output_tokens": sum(e["output_tokens"] for e in stages.values()),
        }
        costs: dict[str, dict] = {}
        warnings: list[str] = []
        for model_key, rates in (reference_pricing or {}).items():
            try:
                rate_in = float(rates["input"])
                rate_out = float(rates["output"])
            except (TypeError, KeyError, ValueError):
                warnings.append(
                    f"reference_pricing[{model_key!r}] malformed (need numeric "
                    "input/output per 1M tokens) — skipped"
                )
                continue
            cost_in = totals["input_tokens"] / 1_000_000 * rate_in
            cost_out = totals["output_tokens"] / 1_000_000 * rate_out
            costs[str(model_key)] = {
                "input_usd": round(cost_in, 6),
                "output_usd": round(cost_out, 6),
                "total_usd": round(cost_in + cost_out, 6),
            }
        out = {
            "schema": LLM_USAGE_SCHEMA,
            "stages": stages,
            "totals": totals,
            "reference_costs_usd": costs,
        }
        if warnings:
            out["pricing_warnings"] = warnings
        return out
