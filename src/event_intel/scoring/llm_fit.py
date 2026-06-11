"""Y1D D1 — LLM-judged capability fit (the default), cosine as the fallback.

Why: bge-m3 dense cosine measured dead-flat on real pairs (GTC×MongoDB target
0.54 vs bad_fit 0.50 — hubness clusters everything near 0.5), so the 0.30-weight
headline dimension carried no ranking signal. An explicit LLM judgment ("would
this exhibitor plausibly need a product with these capabilities?") restores
separation.

Failure handling is per-company: any LLM transport/parsing failure keeps that
company's cosine value (``capability_fit_source`` stays "cosine") and this
stage NEVER fails a build. ``scoring.capability_fit_mode: cosine`` is the
escape hatch that turns the stage off entirely (offline / zero-LLM-cost runs).

stdlib-only at module load (cold-import rule); the LLM arrives as an injected
provider.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.rag.retriever import FitResult

FIT_STAGE = "llm_fit"

FIT_SYSTEM = (
    "You judge product-exhibitor fit for B2B business development. "
    "Reply with ONLY a JSON object, nothing else."
)

# Defensive net for truncated/decorated replies: pull the score even when the
# surrounding JSON does not parse (a long reasoning can hit the max_tokens cap).
_SCORE_RE = re.compile(r'"score"\s*:\s*(-?[0-9]*\.?[0-9]+)')

_MAX_CHUNKS = 3            # capability excerpts per prompt (hits arrive sorted)
_MAX_CHUNK_CHARS = 400     # per-excerpt cap — keeps the prompt ~300-500 tokens
_MAX_EVIDENCE_CHARS = 600
_MAX_TOKENS = 96           # {"score": .., "reasoning": "<=15 words"} fits easily
_NO_EVIDENCE = "(no evidence beyond the name)"


@lru_cache(maxsize=8)
def load_fit_prompt(lang: str) -> str:
    """prompts/{lang}/capability_fit.txt with an en fallback (rescue/analyzer
    pattern). Placeholders are substituted via str.replace (brace-safe — the
    template contains a JSON example).
    """
    here = Path(__file__).resolve().parents[1]  # src/event_intel
    path = here / "prompts" / lang / "capability_fit.txt"
    if not path.is_file():
        path = here / "prompts" / "en" / "capability_fit.txt"
    return path.read_text(encoding="utf-8")


def parse_fit_response(text: str | None) -> tuple[float, str | None] | None:
    """``{"score": x, "reasoning": "..."}`` → (clamped score, reasoning).

    Tolerates fenced/embedded JSON and truncated tails (regex score net).
    NaN/inf scores are rejected — min/max clamping would silently turn NaN
    into 1.0. None = unusable → caller keeps the cosine value.
    """
    if not text:
        return None
    s = text.strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict) and "score" in obj:
            try:
                score = float(obj["score"])
            except (TypeError, ValueError):
                return None
            if not math.isfinite(score):
                return None
            reasoning = obj.get("reasoning")
            if not (isinstance(reasoning, str) and reasoning.strip()):
                reasoning = None
            else:
                reasoning = reasoning.strip()
            return max(0.0, min(1.0, score)), reasoning
    m = _SCORE_RE.search(s)
    if m:
        try:
            score = float(m.group(1))
        except ValueError:
            return None
        if not math.isfinite(score):
            return None
        return max(0.0, min(1.0, score)), None
    return None


def _evidence_text(row: EnrichedExhibitor | None) -> str:
    """Same evidence view the cosine query used (retriever._exhibitor_query_text),
    minus the name (the prompt carries it separately), capped for prompt size.
    """
    if row is None:
        return _NO_EVIDENCE
    parts: list[str] = []
    if row.source_snippet:
        parts.append(row.source_snippet)
    if row.description:
        parts.append(row.description)
    for n in (row.news_signals or [])[:3]:
        if n.title:
            parts.append(n.title)
    joined = " | ".join(parts)[:_MAX_EVIDENCE_CHARS].strip()
    return joined or _NO_EVIDENCE


def compute_llm_capability_fit(
    company_name: str,
    source_snippet: str,
    capability_chunks: list[str],
    llm_provider: object,
    *,
    lang: str = "en",
    ledger: object | None = None,
) -> tuple[float, str | None] | None:
    """One LLM call → (score 0..1, reasoning) or None on ANY failure.

    Usage is recorded into the ledger as soon as a response arrives — even if
    parsing then fails, the tokens were spent.
    """
    if not capability_chunks:
        return None
    caps = "\n---\n".join(c[:_MAX_CHUNK_CHARS] for c in capability_chunks[:_MAX_CHUNKS])
    prompt = (
        load_fit_prompt(lang)
        .replace("{name}", company_name)
        .replace("{evidence}", source_snippet or _NO_EVIDENCE)
        .replace("{capabilities}", caps)
    )
    try:
        resp = llm_provider.chat_once(
            system=FIT_SYSTEM, user=prompt, max_tokens=_MAX_TOKENS, temperature=0.0
        )
    except Exception:  # noqa: BLE001 — per-company fallback, never fail the build
        return None
    if ledger is not None:
        ledger.record(FIT_STAGE, getattr(llm_provider, "model", ""), getattr(resp, "usage", None))
    return parse_fit_response(getattr(resp, "text", None))


def apply_llm_capability_fit(
    *,
    rows: list[EnrichedExhibitor],
    fit_results: list[FitResult],
    llm_provider: object,
    lang: str = "en",
    ledger: object | None = None,
) -> list[str]:
    """Replace each FitResult's cosine capability_fit with the LLM judgment,
    in place. The cosine value is preserved in ``cosine_capability_fit`` and
    ``capability_fit_source`` flips to "llm" only on success.

    Capability chunk texts come from ``fit.top_hits`` (Chroma hits carry
    "document"), filtered to ``metadata.kind == "capability"`` — no extra
    retrieval. Duplicate company names share one call (in-build cache).

    Returns aggregated run warnings (at most one); NEVER raises.
    """
    if not fit_results:
        return []
    rows_by_name = {r.name: r for r in rows}
    cache: dict[str, tuple[float, str | None] | None] = {}
    fallback = 0
    for fit in fit_results:
        try:
            key = fit.name.strip().lower()
            if key in cache:
                verdict = cache[key]
            else:
                chunks = [
                    str(h.get("document"))
                    for h in (fit.top_hits or [])
                    if isinstance(h, dict)
                    and (h.get("metadata") or {}).get("kind") == "capability"
                    and h.get("document")
                ]
                verdict = compute_llm_capability_fit(
                    fit.name,
                    _evidence_text(rows_by_name.get(fit.name)),
                    chunks,
                    llm_provider,
                    lang=lang,
                    ledger=ledger,
                )
                cache[key] = verdict
        except Exception:  # noqa: BLE001 — any surprise keeps the cosine value
            verdict = None
        if verdict is None:
            fallback += 1
            continue
        score, reasoning = verdict
        fit.cosine_capability_fit = fit.capability_fit
        fit.capability_fit = score
        fit.capability_fit_source = "llm"
        fit.capability_fit_reasoning = reasoning
    if fallback:
        return [
            f"llm_fit: {fallback}/{len(fit_results)} companies kept the cosine "
            "capability_fit (LLM fit unavailable or unparseable)"
        ]
    return []
