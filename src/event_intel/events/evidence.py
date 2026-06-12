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
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

OFFICIAL_URL = "official_url"
PRODUCT_PAGE = "product_page"
DOCS = "docs"
PARTNER_PAGE = "partner_page"
PRESS_RELEASE = "press_release"
# Homepage-crawl lane (#16 S4, user-approved news-replacement experiment): the
# company's OWN /news /press /newsroom listing page, verified by a direct
# fetch. Assigned DIRECTLY by events.homepage_evidence — never by
# classify_url_type — so path-based classification of search results is
# unaffected (a search hit on someone's /press path stays PRESS_RELEASE).
PRESS_PAGE = "press_page"
NEWS = "news"

EVIDENCE_TYPES = (
    OFFICIAL_URL, PRODUCT_PAGE, DOCS, PARTNER_PAGE, PRESS_RELEASE, PRESS_PAGE, NEWS,
)

# Highest precedence first — when one canonical URL matches multiple candidate
# types, the earliest in this list wins.
TYPE_PRECEDENCE = [
    NEWS, PRESS_RELEASE, PRESS_PAGE, PARTNER_PAGE, PRODUCT_PAGE, DOCS, OFFICIAL_URL,
]
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
    "co.uk", "org.uk", "ac.uk", "gov.uk", "co.kr", "or.kr", "ac.kr", "go.kr",
    "ne.kr", "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.nz", "ac.nz", "govt.nz",
    "com.br", "co.in", "com.cn", "com.sg", "co.za", "com.mx", "com.tr", "com.hk",
    "com.tw", "co.il", "com.ar", "co.id", "com.my", "co.th", "com.ph", "com.vn",
    "com.co", "com.pe", "com.ua", "co.ke", "com.ng",
    # multi-tenant hosting / site builders (each subdomain = distinct tenant)
    "github.io", "gitlab.io", "github.dev", "vercel.app", "netlify.app",
    "pages.dev", "workers.dev", "web.app", "firebaseapp.com", "herokuapp.com",
    "fly.dev", "onrender.com", "surge.sh", "glitch.me", "repl.co", "replit.app",
    "wixsite.com", "webflow.io", "blogspot.com", "wordpress.com",
    # more managed hosting / site builders + PaaS app subdomains
    "myshopify.com", "azurewebsites.net", "azurestaticapps.net", "amplifyapp.com",
    "cloudfunctions.net", "streamlit.app", "pythonanywhere.com", "gitbook.io",
    "readthedocs.io", "notion.site", "framer.website", "framer.app", "carrd.co",
    "super.site", "bubbleapps.io", "substack.com", "ghost.io", "hashnode.dev",
    "godaddysites.com", "square.site", "weebly.com", "strikingly.com", "tilda.ws",
    "durable.co", "site123.me", "now.sh",
}


def registrable_domain(host: str | None) -> str | None:
    """eTLD+1 heuristic so subdomains collapse to the same site
    (api.acme.com / www.acme.com / acme.com → acme.com), EXCEPT under known
    two-level suffixes where the third label is the registrable unit
    (acme.co.uk; a.github.io stays distinct from b.github.io). Reviews #1 + #7.
    """
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
    """Significant lowercased tokens of a company name for relevance checks.

    Keeps len>=2 tokens (review round-3 #3): the previous len>=3 cut dropped short
    DISTINCTIVE tokens ("Xy Data" → ["data"], losing "xy"), which then matched any
    article mentioning the lone generic "data". Keeping len>=2 means a short
    distinctive token survives to anchor the match, and a name like "Data AI"
    becomes all-generic (["data","ai"]) so mentions_name requires the full phrase
    instead of a single generic word. Falls back to the whole name if nothing
    survives (e.g. all single-char tokens).
    """
    toks = [t for t in _NAME_TOKEN_RE.split((name or "").lower()) if len(t) >= 2]
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

    RESIDUAL LIMITATION (review round-3 #3): a company whose ONLY token is a single
    generic word ("Data", "Cloud") still matches loosely — there is no second token
    to require, and refusing to match would kill recall for a legitimately-named
    "Data" company. Single generic-word names are inherently ambiguous; accepted.
    (The earlier "Data AI" → ["data"] gap is closed: name_tokens now keeps len>=2,
    so the full ["data","ai"] is all-generic and the whole phrase is required.)
    """
    if not tokens or not text:
        return False
    hay = {t for t in _NAME_TOKEN_RE.split(text.lower()) if t}
    distinctive = [t for t in tokens if t not in _GENERIC_NAME_TOKENS]
    if distinctive:
        return any(t in hay for t in distinctive)
    return all(t in hay for t in tokens)


# ---------- entity-relevance gate (news plan C1) ----------
#
# Wrong-entity news ("Dust" the company vs "dust storm" the article) passes the
# whole-token mentions_name gate because the token IS in the text. The risk
# shape is a name whose distinctive tokens reduce to ONE ordinary English word.
# For those names only, is_relevant_news additionally requires >=1 of the
# company's own context terms (from its source_snippet/description) to co-occur
# in the text. Deterministic, fail-open when no context exists. Call sites are
# wired in C2 / the B-lane; C1 keeps these pure and unconnected.

# Small stopword set for context_terms — function words that would make the
# co-occurrence check trivially true.
_CONTEXT_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "has",
    "have", "its", "their", "our", "your", "into", "over", "under", "about",
    "more", "most", "all", "any", "can", "will", "not", "but", "you", "they",
    "than", "then", "when", "where", "which", "who", "how", "what", "why",
    "also", "been", "being", "were", "had", "his", "her", "she", "him", "out",
    "new", "use", "used", "using", "via", "per", "such", "other", "based",
    # Scaffold words from structured snippets ("company: X | description: …")
    # — metadata about the text, not content of it.
    "description", "overview", "profile", "booth", "exhibitor",
}

_COMMON_WORDS_CACHE: frozenset[str] | None = None


def _common_words() -> frozenset[str]:
    """Lazy frozenset of frequent English words (bundled data file; see its
    header for provenance). Import stays stdlib-cold; the file is read once on
    first use. Missing/unreadable file fails OPEN (nothing is "ambiguous").
    """
    global _COMMON_WORDS_CACHE
    if _COMMON_WORDS_CACHE is None:
        path = Path(__file__).resolve().parent / "data" / "common_words.txt"
        words: set[str] = set()
        try:
            with path.open(encoding="utf-8") as f:
                for line in f:
                    w = line.strip().lower()
                    if w and not w.startswith("#"):
                        words.add(w)
        except OSError:
            words = set()
        _COMMON_WORDS_CACHE = frozenset(words)
    return _COMMON_WORDS_CACHE


def name_is_ambiguous(name: str | None) -> bool:
    """True iff the name's DISTINCTIVE tokens reduce to exactly one ordinary
    English word — the homonym-risk shape ("Dust", "Ramp", "Chroma").
    Multi-token names ("LangChain Labs") and coined words ("Baseten") are not
    ambiguous; all-generic names ("Data Cloud") are handled by mentions_name's
    phrase rule instead.
    """
    distinctive = [t for t in name_tokens(name) if t not in _GENERIC_NAME_TOKENS]
    if len(distinctive) != 1:
        return False
    return distinctive[0] in _common_words()


def context_terms(text: str | None) -> set[str]:
    """Content tokens of a company's own snippet/description — its
    disambiguation context ("Dust" + {agents, enterprise, grounded, ...}).
    Stopwords and generic company-name words are excluded so the co-occurrence
    check can't be satisfied by filler.
    """
    if not text:
        return set()
    toks = {t for t in _NAME_TOKEN_RE.split(text.lower()) if len(t) >= 3}
    return {
        t for t in toks
        if t not in _CONTEXT_STOPWORDS and t not in _GENERIC_NAME_TOKENS
    }


def is_relevant_news(
    text: str | None,
    *,
    name: str | None,
    ctx_terms: set[str] | None = None,
    news_domain: str | None = None,
    official_domain: str | None = None,
) -> bool:
    """Entity-relevance gate for one news item (C1; consumers wired in C2/B2).

    1. Same-site as the official domain → relevant (unchanged behavior).
    2. Otherwise the text must mention the name (whole-token, generic-aware).
    3. For AMBIGUOUS names only (single common-word distinctive token), the
       text must additionally co-mention >=1 of the company's ``ctx_terms``.
       Empty/None ctx_terms → fail-open (no context to disambiguate with —
       over-filtering a sparse-snippet company would cost real recall).

    The company's OWN name tokens never count as context (live AIEWF bug,
    2026-06-11): snippets shaped like "company: Dust | description: ..." put
    "dust" into ctx_terms, which every "dust storm" article trivially
    co-mentions — the gate must demand a NON-name disambiguating term.

    Exception: when ALL tokens of a multi-token name appear in the text
    ("Together AI" → both "together" and "ai"), the mention is phrase-strength
    and needs no extra context — only bare single-common-word mentions
    ("Dust", "Ramp") stay under the co-occurrence requirement.
    """
    if official_domain and news_domain and same_site(news_domain, official_domain):
        return True
    toks = name_tokens(name)
    if not mentions_name(text, toks):
        return False
    if not name_is_ambiguous(name):
        return True
    hay = {t for t in _NAME_TOKEN_RE.split((text or "").lower()) if t}
    if len(toks) > 1 and all(t in hay for t in toks):
        return True  # full multi-token name present — phrase-strength mention
    terms = (ctx_terms or set()) - set(toks)
    if not terms:
        return True
    return bool(hay & terms)


def canonical_url(url: str) -> str:
    """Scheme+host+path normalized; drop query/fragment and trailing slash so the
    same page found by different queries dedupes to one key.
    """
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    scheme = parts.scheme.lower() or "https"
    return urlunsplit((scheme, host, path, "", ""))


def classify_url_type(url: str, *, from_news: bool = False) -> str:
    """Type a URL by PATH (query-independent). News-endpoint results default to
    NEWS unless their path is clearly a press/newsroom page.
    """
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
    Preserves published_at/source_domain from the kept (or any) item.
    """
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
    if item.type == PRESS_PAGE:
        # Homepage lane (user-approved scoring-semantics change, 2026-06-11):
        # a verified press/news page on the company's OWN site is the activity
        # signal replacing news search. Same-site ONLY — a third-party
        # press_page shouldn't exist (the type is assigned directly by the
        # crawler), so anything else is defensively not activity. Floor
        # thresholds themselves (2→S/A, 1→A, 0→B) are unchanged.
        return same_site(item.source_domain, official_domain)
    if item.type == PARTNER_PAGE:
        # Independent (third-party-domain) partner page is an activity signal.
        return not same_site(item.source_domain, official_domain)
    return False


def floor_components(row: object) -> tuple[bool, bool]:
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
