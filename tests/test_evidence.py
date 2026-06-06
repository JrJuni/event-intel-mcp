"""Phase 18V item 1 — typed evidence: classification, canonical dedupe,
precedence, identity-vs-activity floor components."""
from __future__ import annotations

from event_intel.events.evidence import (
    EvidenceItem,
    canonical_url,
    classify_url_type,
    domain_of,
    floor_components,
    merge_evidence,
)


def test_classify_url_type_by_path_not_query():
    assert classify_url_type("https://acme.com") == "official_url"
    assert classify_url_type("https://acme.com/products/x") == "product_page"
    assert classify_url_type("https://acme.com/docs/api") == "docs"
    assert classify_url_type("https://acme.com/partners") == "partner_page"
    assert classify_url_type("https://acme.com/press/launch") == "press_release"
    # A homepage returned by a "press release" query is NOT press_release — path wins.
    assert classify_url_type("https://acme.com", from_news=False) == "official_url"
    # News-endpoint result with no press path → news; with press path → press_release.
    assert classify_url_type("https://techblog.com/story", from_news=True) == "news"
    assert classify_url_type("https://acme.com/newsroom/x", from_news=True) == "press_release"


def test_canonical_url_dedupes_variants():
    a = canonical_url("https://www.Acme.com/products/x/")
    b = canonical_url("https://acme.com/products/x?utm=1#frag")
    assert a == b


def test_merge_evidence_dedupes_same_url_with_precedence():
    """Same canonical URL found by 3 queries → ONE item, highest-precedence type
    (news > press_release > ... > official_url) kept (acceptance #3)."""
    raw = [
        EvidenceItem("official_url", "https://acme.com/press/launch", "acme.com"),
        EvidenceItem("product_page", "https://acme.com/press/launch?x=1", "acme.com"),
        EvidenceItem("press_release", "https://www.acme.com/press/launch/", "acme.com", published_at="2026-01-01"),
    ]
    merged = merge_evidence(raw)
    assert len(merged) == 1
    assert merged[0].type == "press_release"
    assert merged[0].published_at == "2026-01-01"


def test_floor_components_identity_vs_activity():
    class _Row:
        def __init__(self, official_url, evidence):
            self.official_url = official_url
            self.news_signals = []
            self.evidence = evidence

    od = "acme.com"
    # identity only (same-site product page) → (True, False)
    r1 = _Row("https://acme.com", [
        EvidenceItem("official_url", "https://acme.com", od),
        EvidenceItem("product_page", "https://acme.com/products", od),
    ])
    assert floor_components(r1) == (True, False)

    # independent partner page (third-party domain) counts as activity
    r2 = _Row("https://acme.com", [
        EvidenceItem("official_url", "https://acme.com", od),
        EvidenceItem("partner_page", "https://bigcorp.com/partners/acme", "bigcorp.com"),
    ])
    assert floor_components(r2) == (True, True)

    # own-domain partner page is identity, not activity
    r3 = _Row("https://acme.com", [
        EvidenceItem("partner_page", "https://acme.com/partners", od),
    ])
    assert floor_components(r3) == (True, False)


def test_domain_of_strips_www():
    assert domain_of("https://www.acme.com/x") == "acme.com"
    assert domain_of(None) is None
