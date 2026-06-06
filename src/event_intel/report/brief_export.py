"""Optional `product_brief.md` export from CapabilityCards.

This is the human-readable "marketing one-pager" view of the same data the
mini-RAG indexes. Useful when a BD rep wants to skim the product framing
without opening the yaml.

Per plan §Context: `product_brief.md` is a *people-facing export view*, not
an alternate SSOT. Edits should go back into capability_cards.yaml.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards


def render_product_brief_md(cards: CapabilityCards, *, lang: str = "en") -> str:
    """Render a one-page Markdown brief from capability cards."""
    L = _LABELS_KO if lang == "ko" else _LABELS_EN

    out: list[str] = []
    out.append(f"# {cards.product_name}")
    out.append("")
    out.append(f"_{cards.one_liner}_")
    out.append("")
    out.append(f"## {L['capabilities']}")
    for cap in cards.capabilities:
        out.append("")
        out.append(f"### {cap.name}")
        out.append(f"- {L['keywords']}: {', '.join(cap.keywords)}")
        out.append(f"- {L['buyer_pains']}:")
        for p in cap.buyer_pains:
            out.append(f"  - {p}")
        out.append(f"- {L['evidence_queries']}:")
        for q in cap.evidence_queries:
            out.append(f"  - `{q}`")

    ic = cards.ideal_customer
    out.append("")
    out.append(f"## {L['ideal_customer']}")
    out.append(f"- {L['industries']}: {', '.join(ic.industries)}")
    out.append(f"- {L['signals']}: {', '.join(ic.company_signals)}")
    if ic.geo:
        out.append(f"- {L['geo']}: {', '.join(ic.geo)}")

    if cards.buying_triggers:
        out.append("")
        out.append(f"## {L['triggers']}")
        for t in cards.buying_triggers:
            out.append(f"- {t.signal} (weight {t.weight:.2f})")

    if cards.bad_fit:
        out.append("")
        out.append(f"## {L['bad_fit']}")
        for bf in cards.bad_fit:
            kw = f" — keywords: {', '.join(bf.keywords)}" if bf.keywords else ""
            out.append(f"- {bf.reason}{kw}")

    if cards.competitors:
        out.append("")
        out.append(f"## {L['competitors']}")
        for c in cards.competitors:
            kw = f" ({', '.join(c.keywords)})" if c.keywords else ""
            out.append(f"- {c.name}{kw}")

    out.append("")
    return "\n".join(out)


_LABELS_EN = {
    "capabilities": "Capabilities",
    "keywords": "keywords",
    "buyer_pains": "buyer pains",
    "evidence_queries": "evidence queries",
    "ideal_customer": "Ideal Customer",
    "industries": "industries",
    "signals": "company signals",
    "geo": "geo",
    "triggers": "Buying Triggers",
    "bad_fit": "Bad Fit",
    "competitors": "Competitors",
}

_LABELS_KO = {
    "capabilities": "역량 (Capabilities)",
    "keywords": "키워드",
    "buyer_pains": "고객 페인",
    "evidence_queries": "증거 검색 쿼리",
    "ideal_customer": "이상적 고객 (Ideal Customer)",
    "industries": "산업",
    "signals": "회사 시그널",
    "geo": "지역",
    "triggers": "구매 트리거",
    "bad_fit": "부적합 (Bad Fit)",
    "competitors": "경쟁사",
}
