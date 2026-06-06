"""Typed evidence items + canonical dedupe + identity-vs-activity floor.

Phase 18V item 1 (+ review round-2 #3, #5). Evidence is no longer just
official_url + news: a company can be backed by product pages, docs, partner
pages, press releases, and news. But more types must NOT trivially lift everyone
to the top floor, so:

- Each distinct URL becomes ONE EvidenceItem. The same URL returned by several
  queries is deduped on its CANONICAL form; ties resolved by a fixed type
  precedence (news > press_release > partner_page > product_page > docs >
  official_url) — classification is by URL PATH, never by which query found it,
  so a homepage returned by a "press release" query is not tagged press_release.
- The floor distinguishes IDENTITY (same-site existence proof: official_url /
  product_page / docs / own-domain partner_page) from ACTIVITY / INDEPENDENT
  (news / press_release / third-party partner_page). Floor 2 requires BOTH —
  official_url + same-site product_page (one identity) cannot reach floor 2.

Pure stdlib — import-cold (scoring.rules depends on this for the floor).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

OFFICIAL_URL = "official_url"
PRODUCT_PAGE = "product_page"
DOCS = "docs"
PARTNER_PAGE = "partner_page"
PRESS_RELEASE = "press_release"
NEWS = "news"

EVIDENCE_TYPES = (OFFICIAL_URL, PRODUCT_PAGE, DOCS, PARTNER_PAGE, PRESS_RELEASE, NEWS)

# Highest precedence first — when one canonical URL matches multiple candidate
# types, the earliest in this list wins.
TYPE_PRECEDENCE = [NEWS, PRESS_RELEASE, PARTNER_PAGE, PRODUCT_PAGE, DOCS, OFFICIAL_URL]
_PRECEDENCE_RANK = {t: i for i, t in enumerate(TYPE_PRECEDENCE)}

_PRESS_RE = re.compile(
    r"/(press|news|newsroom|media|announcements?|press[-_]releases?)(/|$|\?|#)", re.I
)
_DOCS_RE = re.compile(
    r"/(docs?|documentation|developers?|api|reference|changelog)(/|$|\?|#)", re.I
)
_PARTNER_RE = re.compile(
    r"/(partners?|integrations?|marketplace|ecosystem)(/|$|\?|#)", re.I
)
_PRODUCT_RE = re.compile(
    r"/(products?|solutions?|platform|features?|pricing)(/|$|\?|#)", re.I
)


@dataclass
class EvidenceItem:
    type: str
    url: str
    source_domain: str | None = None
    published_at: str | None = None


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def canonical_url(url: str) -> str:
    """Scheme+host+path normalized; drop query/fragment and trailing slash so the
    same page found by different queries dedupes to one key."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    scheme = parts.scheme.lower() or "https"
    return urlunsplit((scheme, host, path, "", ""))


def classify_url_type(url: str, *, from_news: bool = False) -> str:
    """Type a URL by PATH (query-independent). News-endpoint results default to
    NEWS unless their path is clearly a press/newsroom page."""
    path = urlsplit(url).path or "/"
    if _PRESS_RE.search(path):
        return PRESS_RELEASE
    if from_news:
        return NEWS
    if _DOCS_RE.search(path):
        return DOCS
    if _PARTNER_RE.search(path):
        return PARTNER_PAGE
    if _PRODUCT_RE.search(path):
        return PRODUCT_PAGE
    return OFFICIAL_URL


def merge_evidence(raw: list[EvidenceItem]) -> list[EvidenceItem]:
    """Dedupe by canonical URL, keeping the highest-precedence type per URL.
    Preserves published_at/source_domain from the kept (or any) item."""
    best: dict[str, EvidenceItem] = {}
    for item in raw:
        if not item.url:
            continue
        key = canonical_url(item.url)
        cur = best.get(key)
        if cur is None:
            best[key] = item
            continue
        if _PRECEDENCE_RANK.get(item.type, 99) < _PRECEDENCE_RANK.get(cur.type, 99):
            # New type wins; carry over a published_at if the winner lacks one.
            if not item.published_at and cur.published_at:
                item.published_at = cur.published_at
            best[key] = item
        elif not cur.published_at and item.published_at:
            cur.published_at = item.published_at
    return list(best.values())


def _is_identity(item: EvidenceItem, *, official_domain: str | None) -> bool:
    if item.type in (OFFICIAL_URL, PRODUCT_PAGE, DOCS):
        return True
    if item.type == PARTNER_PAGE:
        # Own-domain partner page is just existence proof; independent isn't.
        return official_domain is not None and item.source_domain == official_domain
    return False


def _is_activity(item: EvidenceItem, *, official_domain: str | None) -> bool:
    if item.type in (NEWS, PRESS_RELEASE):
        return True
    if item.type == PARTNER_PAGE:
        return official_domain is None or item.source_domain != official_domain
    return False


def floor_components(row) -> tuple[bool, bool]:
    """(has_identity, has_activity) for a row. Uses the typed evidence list when
    present; otherwise falls back to the legacy official_url (identity) +
    news_signals (activity) representation so pre-item-1 rows score identically.
    """
    evidence = getattr(row, "evidence", None)
    if evidence:
        official_domain = domain_of(getattr(row, "official_url", None))
        identity = any(_is_identity(i, official_domain=official_domain) for i in evidence)
        activity = any(_is_activity(i, official_domain=official_domain) for i in evidence)
        return identity, activity
    return bool(getattr(row, "official_url", None)), bool(getattr(row, "news_signals", None))
