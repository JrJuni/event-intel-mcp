"""GPT-OAuth draft labeling (silver) — Y1 L1 / Stage A.

Bulk first-pass labels from a single LLM (typically GPT via OAuth — the same
vendor as the engine's extraction, so its auto-accepted output is SILVER: fine
for DEV diagnostics, never a holdout gate. Gold needs an independent second
vendor / human / search — see plan silver-vs-gold).

Each company in a labeling sheet gets `suggested_label` + `confidence` +
`rationale`. A row the model can't label, or whose label is outside the vocab,
is marked `needs_review=True` so L2's flagging never auto-accepts a junk draft.

Cold-import: stdlib + an INJECTED `llm_provider` (providers.llm is cold-safe);
no heavy ML at module top. Regression-guarded by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from event_intel.eval.labeling import LABEL_VALUES

if TYPE_CHECKING:
    from event_intel.providers.llm import LLMProvider

_CONF_WORDS = {"low": 0.3, "medium": 0.6, "med": 0.6, "high": 0.9}
# dict-wrapped responses to tolerate (mirrors extraction's unwrap policy).
_LIST_KEYS = ("labels", "companies", "results", "items")


def _load_prompt(lang: str) -> str:
    """Load draft_labels.txt for `lang`, falling back to en."""
    base = Path(__file__).resolve().parents[1] / "prompts"  # src/event_intel/prompts
    for p in (base / lang / "draft_labels.txt", base / "en" / "draft_labels.txt"):
        if p.is_file():
            return p.read_text(encoding="utf-8")
    raise FileNotFoundError(f"draft_labels.txt prompt not found for lang={lang!r}")


def _batched(rows: list[dict[str, Any]], n: int) -> Iterator[list[dict[str, Any]]]:
    n = max(1, n)
    for i in range(0, len(rows), n):
        yield rows[i : i + n]


def _coerce_confidence(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return _CONF_WORDS.get(str(v).strip().lower(), 0.5)


def _parse_draft_response(raw: str, valid_names: set[str]) -> dict[str, dict[str, Any]]:
    """Tolerant JSON parse of one batch response → {name: {label, confidence,
    rationale}} for names in `valid_names`. Strips code fences, unwraps dict
    envelopes, and last-ditch slices the first '[' … last ']' (extraction's pattern).
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}
    if isinstance(data, dict):
        unwrapped: list | None = None
        for key in _LIST_KEYS:
            if isinstance(data.get(key), list):
                unwrapped = data[key]
                break
        if unwrapped is None and len(data) == 1:
            (only,) = data.values()
            if isinstance(only, list):
                unwrapped = only
        if unwrapped is None:
            return {}
        data = unwrapped
    if not isinstance(data, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if name not in valid_names:
            continue
        out[name] = {
            "label": str(item.get("label", "")).strip(),
            "confidence": _coerce_confidence(item.get("confidence", 0.5)),
            "rationale": str(item.get("rationale", "")).strip(),
        }
    return out


def _build_user(batch: list[dict[str, Any]], lang: str) -> str:
    no_ov = "(설명 없음)" if lang == "ko" else "(no description)"
    head = (
        "다음 회사들을 라벨링하세요. 각 회사의 개요만 보고 판단하세요.\n"
        if lang == "ko"
        else "Label the companies below. Judge ONLY from each overview.\n"
    )
    lines = [head]
    for r in batch:
        lines.append(f"- name: {r['name']}\n  overview: {r['overview'] or no_ov}")
    tail = (
        "\n위 회사 각각에 대해 JSON 배열로만 답하세요. 항목: "
        '{"name", "label"(target|competitor|bad_fit|neutral), "confidence"(0~1), "rationale"(짧게)}.'
        if lang == "ko"
        else "\nReturn ONLY a JSON array, one object per company: "
        '{"name", "label"(target|competitor|bad_fit|neutral), "confidence"(0-1), "rationale"(short)}.'
    )
    return "\n".join(lines) + tail


def draft_labels(
    *,
    sheet_rows: list[dict[str, Any]],
    product_header: str,
    llm_provider: LLMProvider,
    batch_size: int = 30,
    lang: str = "ko",
    max_tokens: int = 4096,
) -> list[dict[str, Any]]:
    """Augment each labeling-sheet row with a single-vendor draft (silver).

    Adds `suggested_label` / `confidence` / `rationale` / `source="gpt_draft"`,
    and `needs_review=True` when the draft is missing or its label is invalid (so
    L2 never auto-accepts a junk draft). The sheet's blank `label` is left for the
    human/refiner; this only fills the *suggestion*. Returns NEW row dicts.
    """
    rows = [dict(r) for r in sheet_rows]
    system = _load_prompt(lang).strip() + "\n\n" + product_header
    for batch in _batched(rows, batch_size):
        names = {r["name"] for r in batch}
        try:
            resp = llm_provider.chat_once(
                system=system, user=_build_user(batch, lang),
                max_tokens=max_tokens, temperature=0.0,
            )
            parsed = _parse_draft_response(resp.text, names)
        except Exception:  # noqa: BLE001 — a failed batch flags its rows, never crashes
            parsed = {}
        for r in batch:
            p = parsed.get(r["name"])
            if p and p["label"] in LABEL_VALUES:
                r["suggested_label"] = p["label"]
                r["confidence"] = p["confidence"]
                r["rationale"] = p["rationale"]
                r["needs_review"] = False
            else:  # missing or out-of-vocab → must be reviewed, not auto-accepted
                r["suggested_label"] = ""
                r["confidence"] = 0.0
                r["rationale"] = ""
                r["needs_review"] = True
            r["source"] = "gpt_draft"
    return rows
