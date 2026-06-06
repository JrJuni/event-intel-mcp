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


# Two-level suffixes where the registrable unit is the THIRD label from the end.
# Two flavors, treated identically by the algorithm:
#  - ccTLD second levels (co.uk → acme.co.uk)
#  - MULTI-TENANT hosting suffixes where each subdomain is a SEPARATE company
#    (github.io → a.github.io ≠ b.github.io). Without these, two startups on
#    github.io/vercel.app collapse to one site and the identity gate reopens
#    (review round-2 #7).
# Heuristic (no PSL dependency, to stay cold-start safe); extend as needed.
_TWO_LEVEL_SUFFIXES = {
    # ccTLD second levels
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.kr", "or.kr", "co.jp", "or.jp",
    "com.au", "net.au", "co.nz", "com.br", "co.in", "com.cn", "com.sg",
    "co.za", "com.mx", "com.tr", "com.hk", "com.tw",
    # multi-tenant hosting / site builders (each subdomain = distinct tenant)
    "github.io", "gitlab.io", "github.dev", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "herokuapp.com",
    "fly.dev", "onrender.com", "surge.sh", "glitch.me", "repl.co", "replit.app",
    "wixsite.com", "webflow.io", "blogspot.com", "wordpress.com",
}


def registrable_domain(host: str | None) -> str | None:
    """eTLD+1 heuristic so subdomains collapse to the same site
    (api.acme.com / www.acme.com / acme.com → acme.com), EXCEPT under known
    two-level suffixes where the third label is the registrable unit
    (acme.co.uk; a.github.io stays distinct from b.github.io). Reviews #1 + #7."""
    if not host:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in _TWO_LEVEL_SUFFIXES and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def same_site(a: str | None, b: str | None) -> bool:
    """True iff two hosts share a registrable domain (subdomain-tolerant)."""
    ra, rb = registrable_domain(a), registrable_domain(b)
    return ra is not None and ra == rb


_NAME_TOKEN_RE = re.compile(r"[^a-z0-9가-힣]+")

# Generic words common to many company names — matching ONE alone ("data",
# "cloud", "ai") would let an unrelated article/page count as being about the
# company (review round-2 #1). They only count in combination.
_GENERIC_NAME_TOKENS = {
    "data", "cloud", "ai", "ml", "tech", "labs", "lab", "inc", "app",
    "apps", "systems", "system", "solutions", "solution", "group", "global",
    "digital", "soft", "software", "corp", "platform", "network", "networks",
    "the", "and", "company", "technologies", "technology", "studio",
}


def name_tokens(name: str | None) -> list[str]:
    """Significant lowercased tokens of a company name for relevance checks
    (len>=3, else the whole name)."""
    toks = [t for t in _NAME_TOKEN_RE.split((name or "").lower()) if len(t) >= 3]
    if toks:
        return toks
    whole = (name or "").lower().strip()
    return [whole] if whole else []


def mentions_name(text: str | None, tokens: list[str]) -> bool:
    """Whole-token match (NOT substring) with a generic-token guard.

    A single generic token ("data"/"cloud"/"ai") is too weak to mean "about this
    company", so a match requires at least one DISTINCTIVE token. If the name is
    entirely generic (e.g. "Data Cloud"), require ALL its tokens present
    (phrase-like) rather than any one (review round-2 #1).
    """
    if not tokens or not text:
        return False
    hay = {t for t in _NAME_TOKEN_RE.split(text.lower()) if t}
    distinctive = [t for t in tokens if t not in _GENERIC_NAME_TOKENS]
    if distinctive:
        return any(t in hay for t in distinctive)
    return all(t in hay for t in tokens)


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
    # Identity is existence proof on the company's OWN site — a /products or
    # /docs page on a THIRD-PARTY domain is not the company's identity (review
    # #1: third-party path-only matches must not satisfy the floor).
    if item.type in (OFFICIAL_URL, PRODUCT_PAGE, DOCS, PARTNER_PAGE):
        return official_domain is not None and same_site(item.source_domain, official_domain)
    return False


def _is_activity(item: EvidenceItem, *, official_domain: str | None) -> bool:
    if item.type in (NEWS, PRESS_RELEASE):
        return True
    if item.type == PARTNER_PAGE:
        # Independent (third-party-domain) partner page is an activity signal.
        return not same_site(item.source_domain, official_domain)
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
