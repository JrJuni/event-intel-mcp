"""Labeling aid — Y1 CS9.

Blind labeling needs more than names: a human cannot judge target / competitor /
bad_fit from "ClickHouse" alone. This builds a labeling SHEET that pairs each
packeted company with NEUTRAL, judgment-aiding context drawn from the event
SOURCE (the exhibitor list's own descriptions) — NEVER the engine's score / tier
/ rank, so blindness to the engine verdict is preserved (design v4 §2 step 4,
SK-2). A product header (from the capability card) gives the labeler the rubric:
what we sell, who an ideal customer is, who the product team treats as a
competitor — all of it INPUT shared with the engine, none of it engine OUTPUT.

The sheet is fillable + parseable: each row carries the overview inline and an
empty `label` field. The labeler edits `label` in place; `parse_filled_sheet`
reads it back to the {name: label} map that seal_company_labels freezes.

Pure stdlib — import-cold, regression-guarded by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_WS_RE = re.compile(r"\s+")
LABEL_VALUES = ("target", "competitor", "bad_fit", "neutral")
_OVERVIEW_MAX = 600


def _clean(text: Any, *, limit: int = _OVERVIEW_MAX) -> str:
    """Collapse whitespace + trim. None/empty → '' (caller substitutes a notice)."""
    if not text:
        return ""
    s = _WS_RE.sub(" ", str(text)).strip()
    return s[: limit - 1] + "…" if len(s) > limit else s


@dataclass
class CompanyContext:
    name: str
    overview: str = ""
    url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def build_context_from_records(
    records: list[dict[str, Any]],
    *,
    name_key: str,
    overview_keys: tuple[str, ...],
    url_key: str | None = None,
    extra_keys: tuple[str, ...] = (),
) -> dict[str, CompanyContext]:
    """Extract neutral per-company context from raw source records.

    `overview_keys` is tried in order; the first non-empty one wins (so a record
    with an empty `pr` but a present `introduction` still gets an overview). The
    key space is the company NAME (matching the packet's entry names).
    """
    out: dict[str, CompanyContext] = {}
    for rec in records:
        name = str(rec.get(name_key, "")).strip()
        if not name:
            continue
        overview = ""
        for k in overview_keys:
            overview = _clean(rec.get(k))
            if overview:
                break
        url = str(rec[url_key]).strip() if url_key and rec.get(url_key) else None
        extra = {k: rec[k] for k in extra_keys if rec.get(k) is not None}
        out[name] = CompanyContext(name=name, overview=overview, url=url, extra=extra)
    return out


def product_header_from_card(card: dict[str, Any], *, lang: str = "ko") -> str:
    """Render the rubric header from a capability card — product input, not engine
    output. Includes the product team's competitor list (shared input) so the
    labeler can confirm or override it with their own judgment.
    """
    name = card.get("product_name", "?")
    one_liner = _clean(card.get("one_liner"), limit=300)
    caps = [c.get("name", "") for c in card.get("capabilities", []) if c.get("name")]
    ic = card.get("ideal_customer", {}) or {}
    industries = ic.get("industries", []) or []
    comps = [c.get("name", "") for c in card.get("competitors", []) if c.get("name")]
    bad = [_clean(b.get("reason"), limit=80) for b in card.get("bad_fit", []) if b.get("reason")]

    if lang == "ko":
        lines = [
            f"**제품**: {name}",
            f"**한 줄 소개**: {one_liner}",
            f"**핵심 역량**: {', '.join(caps) or '—'}",
            f"**이상적 고객(산업)**: {', '.join(industries) or '—'}",
            f"**제품팀이 보는 경쟁사**: {', '.join(comps) or '—'}  ← 참고용; 본인 판단으로 확인/수정",
            f"**부적합 기준**: {'; '.join(bad) or '—'}",
        ]
    else:
        lines = [
            f"**Product**: {name}",
            f"**One-liner**: {one_liner}",
            f"**Capabilities**: {', '.join(caps) or '—'}",
            f"**Ideal customer (industries)**: {', '.join(industries) or '—'}",
            f"**Competitors (product team's view)**: {', '.join(comps) or '—'}  ← reference; confirm with your own judgment",
            f"**Bad-fit criteria**: {'; '.join(bad) or '—'}",
        ]
    return "\n".join(lines)


def build_labeling_sheet(
    packet_entries: list[dict[str, Any]],
    context_by_name: dict[str, CompanyContext],
) -> list[dict[str, Any]]:
    """One fillable row per packet entry: index/name/overview/url + empty label.

    Carries ONLY neutral context — the packet entries themselves are names-only
    (no score/tier/rank), and context comes from the source, so nothing here
    leaks the engine verdict.
    """
    rows: list[dict[str, Any]] = []
    for e in packet_entries:
        ctx = context_by_name.get(e["name"])
        rows.append(
            {
                "index": e["index"],
                "name": e["name"],
                "overview": ctx.overview if ctx else "",
                "url": (ctx.url if ctx else None),
                "label": "",  # labeler fills: target|competitor|bad_fit|neutral
            }
        )
    return rows


def render_worksheet_md(
    *,
    pair: str,
    product_header: str,
    sheet: list[dict[str, Any]],
    lang: str = "ko",
) -> str:
    """Human-readable worksheet for skimming. The machine-readable fill target is
    the sheet JSON; this is the comfortable read-alongside view.
    """
    no_overview = "(소스에 설명 없음)" if lang == "ko" else "(no description in source)"
    head = "라벨링 워크시트" if lang == "ko" else "Labeling worksheet"
    rubric = "제품 컨텍스트 (판단 기준)" if lang == "ko" else "Product context (rubric)"
    listing = f"회사 목록 ({len(sheet)}개)" if lang == "ko" else f"Companies ({len(sheet)})"
    label_line = "라벨" if lang == "ko" else "label"

    parts = [
        f"# {head} — {pair}",
        "",
        f"## {rubric}",
        product_header,
        "",
        f"**라벨 값**: `{'` / `'.join(LABEL_VALUES)}`"
        if lang == "ko"
        else f"**Label values**: `{'` / `'.join(LABEL_VALUES)}`",
        "",
        f"## {listing}",
    ]
    for row in sheet:
        parts.append(f"\n### [{row['index']}] {row['name']}")
        parts.append(f"- {'개요' if lang == 'ko' else 'overview'}: {row['overview'] or no_overview}")
        if row.get("url"):
            parts.append(f"- URL: {row['url']}")
        parts.append(f"- {label_line}: `______`")
    return "\n".join(parts) + "\n"


def parse_filled_sheet(
    sheet: list[dict[str, Any]], *, require_all: bool = True
) -> dict[str, str]:
    """Read a filled sheet back to {name: label}, validating the label vocab.

    Raises ValueError on an invalid label, or (when require_all) on any blank —
    so a half-finished sheet can't be silently sealed with missing judgments.
    """
    labels: dict[str, str] = {}
    blank: list[str] = []
    bad: list[tuple[str, str]] = []
    for row in sheet:
        name = row.get("name", "")
        val = (row.get("label") or "").strip()
        if not val:
            blank.append(name)
            continue
        if val not in LABEL_VALUES:
            bad.append((name, val))
            continue
        labels[name] = val
    if bad:
        raise ValueError(f"invalid labels {bad}; allowed {LABEL_VALUES}")
    if require_all and blank:
        raise ValueError(
            f"{len(blank)} companies still unlabeled (e.g. {blank[:3]}); "
            "fill every `label` or pass require_all=False"
        )
    return labels
