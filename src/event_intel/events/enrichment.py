"""Enrich extracted exhibitor candidates with official URL + news signals.

Per plan v0.5 §S4 + Contract #11:
    - **Deterministic official URL pick** — query Brave web search for
      `"{name}" official site`, then score candidate URLs against the
      exhibitor name (host similarity, domain rules — no LLM). If the
      candidate already had a URL from extraction (e.g. CSV), trust it.
    - **News signals** — query Brave news for `"{name}"` within
      `news_days_back` days, keep top N as `news_signals`.
    - **Per-call cache** — keyed by sha1(query + kind + lang). Identical
      re-runs hit cache with zero search calls (cost guard).
    - **Per-row resume artifact** — JSONL written one line per exhibitor
      after a row finishes. Subsequent runs with `resume_from` skip rows
      already in the artifact.

The enrichment stage promotes rows from `raw_extraction` state to `enriched`
state (Contract #9). It does NOT score — scoring is `scoring/compute.py`.

Heavy deps stay lazy. Providers are injected so tests pass fakes.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage

if TYPE_CHECKING:
    from event_intel.events.evidence import EvidenceItem
    from event_intel.events.extraction import ExhibitorCandidate
    from event_intel.providers.search import SearchProvider, SearchResult


# Bump when enrichment parsing/filtering semantics change so stale on-disk
# search cache + resume rows are invalidated instead of silently reused.
#   v1 → original.
#   v2 → Brave news parser fix (top-level results) + published_at + non-article
#        news path filter (Phase 18U). Old v1 entries cached empty news.
#   v3 → typed evidence (official_url/product_page/docs/partner_page/press_release/
#        news) + canonical dedupe + UTC-aware published_at (Phase 18V item 1).
#   v4 → cache payload wrapped with `cached_at` (TTL freshness) + resume rows carry
#        `enriched_at` + `input_fp` so changed name/url/snippet/confidence/config
#        re-enrich instead of being skipped forever (Phase 18W P2-1).
#   v5 → search-provider awareness (zero-config plan): the active provider's
#        cache_signature is folded into the cache key + config fingerprint, so
#        switching backend (e.g. brave→ddgs) never reuses another engine's results
#        for the same query/kind/count/days (blind review R1#1).
#   v6 → degraded results no longer stick (news plan N1): rate-limit-degraded
#        empty results are not cached and resume rows carry `degraded` (never
#        reused). The bump flushes pre-N1 poisoned empty caches + un-flagged
#        resume rows that may hide degraded empties.
ENRICH_CACHE_VERSION = 6


def _is_fresh(timestamp_raw: str | None, *, now: datetime, ttl_days: int | None) -> bool:
    """Shared TTL freshness check for the search cache + resume rows.

    Contract (Phase 18W P2-1):
      - ttl_days None or < 0 → infinite (always fresh).
      - ttl_days == 0        → always stale (never reuse).
      - ttl_days > 0         → fresh iff age <= ttl_days.
    Unparseable or future timestamps are treated as stale (conservative — re-fetch
    rather than trust a bad clock).
    """
    if ttl_days is None or ttl_days < 0:
        return True
    if ttl_days == 0:
        return False
    from event_intel.timeutil import normalize_utc, parse_iso_utc

    dt = parse_iso_utc(timestamp_raw)
    if dt is None:
        return False
    age_days = (normalize_utc(now) - dt).total_seconds() / 86400.0
    if age_days < 0:
        return False
    return age_days <= ttl_days


def _config_fingerprint(enrichment_cfg: dict, *, provider_sig: str = "") -> str:
    """Hash ONLY the enrichment-affecting config fields (review r2 #3). A scoring
    weight change must NOT invalidate cached enrichment — only fields that change
    what we fetch/keep belong here. ``provider_sig`` is included so a search-backend
    switch re-enriches instead of reusing another engine's rows (blind review R1#1).
    """
    relevant = {
        "provider": provider_sig,
        "max_companies": enrichment_cfg.get("max_companies"),
        "count_web": enrichment_cfg.get("count_web", enrichment_cfg.get("brave_count_web")),
        "count_news": enrichment_cfg.get("count_news", enrichment_cfg.get("brave_count_news")),
        "news_days_back": enrichment_cfg.get("news_days_back"),
        "official_url_levenshtein_threshold": enrichment_cfg.get(
            "official_url_levenshtein_threshold"
        ),
        "evidence_queries": enrichment_cfg.get("evidence_queries", {}) or {},
    }
    blob = json.dumps(relevant, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()[:16]


def _input_fingerprint(name: str, url: str | None, snippet: str,
                       confidence: float, config_fp: str) -> str:
    """Per-row fingerprint: changed name/url/snippet/confidence/config → re-enrich
    regardless of resume TTL (review r2 #3).
    """
    raw = f"{name}|{url or ''}|{snippet}|{confidence}|{config_fp}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]

# News results whose URL path is a utility/non-article page are dropped — they
# are not real buying signals. We filter by PATH, not domain, so a company's own
# newsroom press release (launch/funding/partnership) is kept (review #2 P2-6).
_NON_ARTICLE_PATH_RE = re.compile(
    r"/(login|sign[-_]?in|sign[-_]?up|signup|status|privacy|terms|tos|"
    r"docs?|documentation|changelog|cookies?|legal|pricing)(/|$|\?|#)",
    re.I,
)


def _is_article_like(url: str) -> bool:
    return not _NON_ARTICLE_PATH_RE.search(url or "")


# ---------- public dataclasses ----------


@dataclass
class NewsSignal:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: str | None = None       # ISO 8601 string, best-effort


@dataclass
class EnrichedExhibitor:
    name: str
    source_snippet: str
    url: str | None = None                # exhibitor-supplied OR enriched
    official_url: str | None = None       # post-enrichment determination
    description: str | None = None
    news_signals: list[NewsSignal] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)  # typed, deduped (18V item 1)
    extraction_confidence: float = 1.0
    enrichment_status: str = "enriched"   # "enriched" | "needs_review" | "failed"
    enrichment_warnings: list[str] = field(default_factory=list)
    # True iff at least one search query for this row degraded to empty (e.g.
    # rate-limit). Degraded rows are persisted for durability but never reused
    # from resume, so the next run retries them (news plan N1).
    degraded: bool = False


@dataclass
class EnrichmentResult:
    rows: list[EnrichedExhibitor]
    cache_hits: int
    cache_misses: int
    skipped_from_resume: int
    warnings: list[str]


# ---------- cache ----------


class _SearchCache:
    """Lightweight on-disk cache. One JSON file per (query, kind, lang, count,
    days) hash. count/days are part of the key (review #4): a news query for the
    last 30 days must NOT serve a cached 180-day result, and a count=5 request
    must not return a count=20 payload.

    Each file is `{"cached_at": iso, "results": [...]}` (v4) so `ttl_days` can
    expire stale Brave answers — a cached "last 180 days" result reused months
    later silently misses everything published since (review r2 #2).
    """

    def __init__(
        self, root: Path, *, ttl_days: int | None = None, provider_sig: str = ""
    ) -> None:
        self.root = root
        self.ttl_days = ttl_days
        # Active search backend's cache_signature — part of the key so a
        # brave-cached result is never served to a ddgs run (blind review R1#1).
        self.provider_sig = provider_sig
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, query: str, kind: str, lang: str, count: int = 0, days: int | None = None) -> str:
        # Version prefix → a parser/semantics bump (ENRICH_CACHE_VERSION) yields
        # new keys, so stale entries (e.g. v1's empty news) are never reused.
        # provider_sig → cross-backend isolation (R1#1).
        h = hashlib.sha1(
            f"v{ENRICH_CACHE_VERSION}|p{self.provider_sig}|{kind}|{lang}|c{count}|d{days}|{query}".encode()
        ).hexdigest()
        return h

    def get(
        self, query: str, *, kind: str, lang: str, count: int = 0,
        days: int | None = None, now: datetime,
    ) -> list[dict] | None:
        path = self.root / f"{self._key(query, kind, lang, count, days)}.json"
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        # v4 wrapper only. A bare list (pre-v4) can't reach here anyway because the
        # version is in the key, but guard defensively → stale.
        if not isinstance(payload, dict):
            return None
        if not _is_fresh(payload.get("cached_at"), now=now, ttl_days=self.ttl_days):
            return None
        return payload.get("results")

    def put(
        self, query: str, *, kind: str, lang: str, results: list[dict],
        count: int = 0, days: int | None = None, now: datetime,
    ) -> None:
        from event_intel.timeutil import normalize_utc

        path = self.root / f"{self._key(query, kind, lang, count, days)}.json"
        payload = {"cached_at": normalize_utc(now).isoformat(), "results": results}
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except OSError:
            pass  # cache is best-effort; ignore disk hiccups


# ---------- resume artifact ----------


class _ResumeStore:
    """JSONL — one row per enriched exhibitor, keyed by name. Append-only.

    Reading is fault-tolerant: a half-written line at EOF is ignored.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_done(self) -> dict[str, dict]:
        if not self.path.is_file():
            return {}
        done: dict[str, dict] = {}
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # partial write — skip
                # Resume rows from an older enrichment version carry stale
                # parsing (e.g. v1's empty news) — drop them so they re-enrich.
                if row.get("_cache_version") != ENRICH_CACHE_VERSION:
                    continue
                name = row.get("name")
                if name:
                    done[name] = row
        return done

    def append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------- URL scoring (deterministic) ----------


_TOKEN_STOPWORDS = {"the", "inc", "inc.", "ltd", "ltd.", "co", "co.", "llc",
                    "corp", "corp.", "주식회사", "(주)", "㈜"}
_BAD_HOST_SUBSTRINGS = (
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "wikipedia.org",
    "youtube.com",
    "crunchbase.com",
    "bloomberg.com",
)


def _host_of(url: str) -> str:
    # Cheap host parse — avoids urllib.parse for speed on hot path.
    m = re.match(r"^https?://([^/]+)/?", url, flags=re.I)
    if not m:
        return ""
    return m.group(1).lower().removeprefix("www.")


def _name_tokens(name: str) -> list[str]:
    base = re.sub(r"[^a-zA-Z0-9가-힣\s]", " ", name).lower()
    return [t for t in base.split() if t and t not in _TOKEN_STOPWORDS]


def _score_candidate_url(name: str, candidate: SearchResult, *, threshold: float) -> float:
    """0..1 score that `candidate.url` is the official site for `name`.

    Cheap features:
      - host contains a name token (heavy weight)
      - difflib ratio between host (without TLD) and a joined name slug
      - LinkedIn/FB/Wikipedia/etc. get a hard penalty
    """
    host = _host_of(candidate.url)
    if not host:
        return 0.0
    for bad in _BAD_HOST_SUBSTRINGS:
        if bad in host:
            return 0.0

    tokens = _name_tokens(name)
    if not tokens:
        return 0.0
    host_stem = host.rsplit(".", 1)[0]  # drop TLD
    slug = "".join(tokens)

    ratio = difflib.SequenceMatcher(a=host_stem, b=slug).ratio()
    token_hit = any(t in host_stem for t in tokens)

    score = ratio
    if token_hit:
        score = min(1.0, score + 0.25)
    # Below threshold gets clamped to 0 so we don't return a low-confidence pick.
    if score < threshold:
        return 0.0
    return score


def _pick_official_url(
    name: str, web_hits: list[SearchResult], *, threshold: float
) -> str | None:
    best_url: str | None = None
    best_score = 0.0
    for hit in web_hits:
        s = _score_candidate_url(name, hit, threshold=threshold)
        if s > best_score:
            best_score = s
            best_url = hit.url
    return best_url


# ---------- main entry ----------


def _searchresult_to_dict(r: SearchResult) -> dict:
    return {
        "title": r.title,
        "url": r.url,
        "snippet": r.snippet,
        "source": r.source,
        "published_at": r.published_at.isoformat() if r.published_at else None,
    }


def _dict_to_searchresult(d: dict) -> SearchResult:
    from event_intel.providers.search import SearchResult
    from event_intel.timeutil import parse_iso_utc

    # Normalize cache-restored timestamps to aware UTC too — a v2 cache written
    # before the normalization fix may hold a naive ISO string (review r2 #1).
    published_at = parse_iso_utc(d.get("published_at"))
    return SearchResult(
        title=d.get("title", ""),
        url=d.get("url", ""),
        snippet=d.get("snippet", ""),
        source=d.get("source"),
        published_at=published_at,
    )


def _to_dict(
    row: EnrichedExhibitor, *, input_fp: str | None = None, enriched_at: str | None = None
) -> dict:
    return {
        "name": row.name,
        "source_snippet": row.source_snippet,
        "url": row.url,
        "official_url": row.official_url,
        "description": row.description,
        "news_signals": [
            {"title": n.title, "url": n.url, "snippet": n.snippet,
             "source": n.source, "published_at": n.published_at}
            for n in row.news_signals
        ],
        "evidence": [
            {"type": e.type, "url": e.url, "source_domain": e.source_domain,
             "published_at": e.published_at}
            for e in row.evidence
        ],
        "extraction_confidence": row.extraction_confidence,
        "enrichment_status": row.enrichment_status,
        "enrichment_warnings": row.enrichment_warnings,
        "degraded": row.degraded,
        "_cache_version": ENRICH_CACHE_VERSION,
        "input_fp": input_fp,
        "enriched_at": enriched_at,
    }


def _from_dict(d: dict) -> EnrichedExhibitor:
    return EnrichedExhibitor(
        name=d["name"],
        source_snippet=d.get("source_snippet", ""),
        url=d.get("url"),
        official_url=d.get("official_url"),
        description=d.get("description"),
        news_signals=[
            NewsSignal(
                title=n.get("title", ""),
                url=n.get("url", ""),
                snippet=n.get("snippet", ""),
                source=n.get("source"),
                published_at=n.get("published_at"),
            )
            for n in d.get("news_signals", [])
        ],
        evidence=_evidence_from_dicts(d.get("evidence", [])),
        extraction_confidence=float(d.get("extraction_confidence", 1.0)),
        enrichment_status=d.get("enrichment_status", "enriched"),
        enrichment_warnings=list(d.get("enrichment_warnings", [])),
        degraded=bool(d.get("degraded", False)),
    )


def _evidence_from_dicts(items: list[dict]) -> list[EvidenceItem]:
    from event_intel.events.evidence import EvidenceItem

    return [
        EvidenceItem(
            type=i.get("type", ""),
            url=i.get("url", ""),
            source_domain=i.get("source_domain"),
            published_at=i.get("published_at"),
        )
        for i in items
    ]


def allocate_round_robin(
    company_names: list[str], suffixes: list[str], *,
    per_company_cap: int, event_cap: int,
) -> dict[str, list[str]]:
    """Fairly distribute extra evidence-query slots across companies (Phase 18W
    P2-2, review r2 #6). An event-wide cap consumed greedily in company order
    starves later companies; round-robin gives every company its 1st slot before
    any gets its 2nd.

    - per_company_cap: max suffixes per company (0 = no cap → all suffixes).
    - event_cap: max total queries across the event (0 = unlimited → every company
      gets its full per-company allowance; this is the default, equivalent to the
      pre-P2-2 per-company-only behavior).
    Deterministic: company order + suffix order are fixed inputs, so the allocation
    is independent of cache warmth and API timing.
    """
    per = len(suffixes) if not per_company_cap else min(per_company_cap, len(suffixes))
    allowed = suffixes[:per]
    if not event_cap:
        return {name: list(allowed) for name in company_names}
    assigned: dict[str, list[str]] = {name: [] for name in company_names}
    total = 0
    for rank in range(per):
        for name in company_names:
            if total >= event_cap:
                return assigned
            assigned[name].append(allowed[rank])
            total += 1
    return assigned


def enrich_exhibitors(
    *,
    candidates: list[ExhibitorCandidate],
    workspace_id: str,
    lang: str = "en",
    config: dict,
    search_provider: SearchProvider,
    cache_dir: Path | None = None,
    resume_path: Path | None = None,
    max_companies: int | None = None,
    refresh: bool = False,
    now: datetime | None = None,
) -> EnrichmentResult:
    """Enrich a list of extracted candidates with official URL + news.

    `cache_dir` defaults to `~/.event-intel/cache/search/{workspace_id}/`.
    `resume_path` defaults to `~/.event-intel/resume/{workspace_id}.jsonl`.

    `refresh=True` bypasses BOTH the resume artifact and the search cache reads —
    a real refresh, not just resume (review r2 #3). Fresh results are still
    written back to the cache. `now` (default `datetime.now(UTC)`) is injected so
    TTL freshness is deterministic in tests.
    """
    now = now or datetime.now(UTC)
    try:
        enrichment_cfg = config["enrichment"]
        max_default = int(enrichment_cfg["max_companies"])
        # Provider-neutral keys (R1#7); legacy brave_count_* still read for
        # back-compat. .get(new, .get(old)) → None if neither → TypeError → the
        # except below raises CONFIG_ERROR.
        count_web = int(enrichment_cfg.get("count_web", enrichment_cfg.get("brave_count_web")))
        count_news = int(enrichment_cfg.get("count_news", enrichment_cfg.get("brave_count_news")))
        news_days = int(enrichment_cfg["news_days_back"])
        cache_enabled = bool(enrichment_cfg.get("cache_enabled", True))
        url_threshold = float(enrichment_cfg["official_url_levenshtein_threshold"])
        # TTL freshness (Phase 18W P2-1). None → infinite; 0 → always stale.
        cache_ttl_days = enrichment_cfg.get("cache_ttl_days")
        resume_ttl_days = enrichment_cfg.get("resume_ttl_days")
        cache_ttl_days = int(cache_ttl_days) if cache_ttl_days is not None else None
        resume_ttl_days = int(resume_ttl_days) if resume_ttl_days is not None else None
        # Extra evidence-type queries (Phase 18V item 1). Default OFF when the key
        # is absent so existing callers/tests keep their exact search budget;
        # shipped defaults.yaml turns them on, capped per event (round-1 #7).
        ev_cfg = enrichment_cfg.get("evidence_queries", {}) or {}
        ev_enabled = {
            "product": (bool(ev_cfg.get("product", False)), "product"),
            "partners": (bool(ev_cfg.get("partners", False)), "partners"),
            "press_release": (bool(ev_cfg.get("press_release", False)), "press release"),
        }
        # Budget is per-company (deterministic, order-independent — review #3) with
        # an OPTIONAL event-wide ceiling (0 = off). When the ceiling is set, slots
        # are allocated round-robin across companies (P2-2) so later companies are
        # not starved by earlier ones; the queried set stays cache-independent.
        ev_max_per_company = int(ev_cfg.get("max_per_company", 3))
        ev_max_extra = int(ev_cfg.get("max_extra_calls_per_event", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.ENRICHMENT,
            message=f"missing or invalid enrichment config: {exc}",
            hint={"required": [
                "enrichment.max_companies", "enrichment.count_web",
                "enrichment.count_news", "enrichment.news_days_back",
                "enrichment.official_url_levenshtein_threshold",
            ]},
        ) from exc

    # Active search backend signature → cache/resume isolation across providers
    # (blind review R1#1). Fakes without the attribute fall back to "" (no-op).
    provider_sig = getattr(search_provider, "cache_signature", "")
    config_fp = _config_fingerprint(enrichment_cfg, provider_sig=provider_sig)
    home = Path.home() / ".event-intel"
    cache_root = cache_dir or (home / "cache" / "search" / workspace_id)
    resume_file = resume_path or (home / "resume" / f"{workspace_id}.jsonl")
    cache = _SearchCache(cache_root, ttl_days=cache_ttl_days, provider_sig=provider_sig)
    resume = _ResumeStore(resume_file)

    # refresh bypasses resume entirely; otherwise reuse is gated per-candidate
    # below (input_fp match AND TTL fresh).
    done_by_name = {} if refresh else resume.load_done()
    cap = max_companies or max_default
    capped = candidates[:cap]
    warnings: list[str] = []
    if "brave_count_web" in enrichment_cfg or "brave_count_news" in enrichment_cfg:
        warnings.append(
            "config uses legacy enrichment.brave_count_* keys — rename to "
            "count_web / count_news (provider-neutral)"
        )
    if len(candidates) > cap:
        warnings.append(
            f"capped enrichment at {cap}/{len(candidates)} exhibitors "
            "(set enrichment.max_companies in config to raise)"
        )

    # Decide reuse vs re-enrich per candidate up front (P2-1 fp + TTL gate): a row
    # is reusable only if inputs are unchanged AND within resume TTL. This lets the
    # round-robin evidence budget (P2-2) be allocated ONLY over companies we will
    # actually enrich — skipped ones must not consume the event-wide query budget.
    fp_by_name: dict[str, str] = {}
    reusable: dict[str, dict] = {}
    to_enrich_names: list[str] = []
    for cand in capped:
        expected_fp = _input_fingerprint(
            cand.name, cand.url, cand.source_snippet,
            cand.extraction_confidence, config_fp,
        )
        fp_by_name[cand.name] = expected_fp
        done_row = done_by_name.get(cand.name)
        if (
            done_row is not None
            and done_row.get("input_fp") == expected_fp
            and _is_fresh(done_row.get("enriched_at"), now=now, ttl_days=resume_ttl_days)
            # Degraded rows (rate-limit empties) are persisted for durability but
            # never reused — the next run must retry them (news plan N1).
            and not done_row.get("degraded", False)
        ):
            reusable[cand.name] = done_row
        else:
            to_enrich_names.append(cand.name)

    enabled_suffixes = [suffix for enabled, suffix in ev_enabled.values() if enabled]
    assigned_queries = allocate_round_robin(
        to_enrich_names, enabled_suffixes,
        per_company_cap=ev_max_per_company, event_cap=ev_max_extra,
    )

    cache_hits = 0
    cache_misses = 0
    skipped = 0
    error_queries = 0
    rows: list[EnrichedExhibitor] = []

    def _tally(hits: dict, row: EnrichedExhibitor) -> None:
        """Fold one _search_with_cache result into counters + row state (N2)."""
        nonlocal cache_hits, cache_misses, error_queries
        if hits["was_hit"]:
            cache_hits += 1
        else:
            cache_misses += 1
        if hits["degraded"]:
            row.degraded = True
            err = hits.get("error")
            if err:
                error_queries += 1
                row.enrichment_warnings.append(f"search error (degraded to empty): {err}")

    for cand in capped:
        if cand.name in reusable:
            rows.append(_from_dict(reusable[cand.name]))
            skipped += 1
            continue

        row = EnrichedExhibitor(
            name=cand.name,
            source_snippet=cand.source_snippet,
            url=cand.url,
            description=cand.description,
            extraction_confidence=cand.extraction_confidence,
        )

        # 1) Official URL — trust extraction URL if present, else search.
        if cand.url:
            row.official_url = cand.url
        else:
            web_query = f'"{cand.name}" official site'
            web_hits = _search_with_cache(
                cache=cache, cache_enabled=cache_enabled,
                search_provider=search_provider, query=web_query,
                kind="web", count=count_web, lang=lang,
                hits_counter=(lambda hit: None),
                now=now, refresh=refresh,
            )
            _tally(web_hits, row)
            picked = _pick_official_url(cand.name, web_hits["results"], threshold=url_threshold)
            row.official_url = picked
            if picked is None and web_hits["results"]:
                row.enrichment_warnings.append(
                    f"web search returned {len(web_hits['results'])} hits but none "
                    f"scored above {url_threshold} for official-site detection"
                )

        # 2) News signals
        news_query = f'"{cand.name}"'
        news_hits = _search_with_cache(
            cache=cache, cache_enabled=cache_enabled,
            search_provider=search_provider, query=news_query,
            kind="news", count=count_news, lang=lang, days=news_days,
            hits_counter=(lambda hit: None),
            now=now, refresh=refresh,
        )
        _tally(news_hits, row)
        for hit in news_hits["results"]:
            # Drop utility/non-article pages (login/docs/privacy…) — not real
            # buying signals. Filter by path, so newsroom press releases stay.
            if not _is_article_like(hit.url):
                continue
            row.news_signals.append(
                NewsSignal(
                    title=hit.title, url=hit.url, snippet=hit.snippet,
                    source=hit.source,
                    published_at=hit.published_at.isoformat() if hit.published_at else None,
                )
            )

        # 3) Typed evidence (Phase 18V item 1): classify official_url + news,
        #    optionally enrich with budgeted product/partner/press queries, then
        #    canonical-dedupe with type precedence.
        from event_intel.events.evidence import (
            EvidenceItem,
            classify_url_type,
            domain_of,
            mentions_name,
            merge_evidence,
            name_tokens,
            same_site,
        )

        official_domain = domain_of(row.official_url)
        cand_name_tokens = name_tokens(cand.name)

        def _evidence_relevant(
            url: str, title: str,
            *, official_domain: str | None = official_domain,
            cand_name_tokens: list[str] = cand_name_tokens,
        ) -> bool:
            # Extra-query results come from arbitrary domains; accept only if the
            # page is plausibly ABOUT this company — same site as the official URL
            # OR a company-name token appears in the host/path/title. Stops a
            # third-party "/products" page from becoming identity (review #1).
            # Loop vars bound as defaults so the closure captures THIS iteration.
            dom = domain_of(url)
            if official_domain and same_site(dom, official_domain):
                return True
            return mentions_name(f"{dom or ''} {url} {title or ''}", cand_name_tokens)

        raw_ev: list[EvidenceItem] = []
        if row.official_url:
            raw_ev.append(
                EvidenceItem(
                    type=classify_url_type(row.official_url),
                    url=row.official_url,
                    source_domain=domain_of(row.official_url),
                )
            )
        for n in row.news_signals:
            # Gate news → floor evidence by relevance too (review round-2 #1):
            # the news query is name-quoted but Brave isn't exact, so an
            # off-topic article shouldn't let official_url + 1 article reach
            # floor 2. news_signals still feed buying_signal (soft-downweighted).
            if not (
                mentions_name(f"{n.title or ''} {n.snippet or ''}", cand_name_tokens)
                or (official_domain and same_site(domain_of(n.url), official_domain))
            ):
                continue
            raw_ev.append(
                EvidenceItem(
                    type=classify_url_type(n.url, from_news=True),
                    url=n.url,
                    source_domain=domain_of(n.url),
                    published_at=n.published_at,
                )
            )
        # Extra evidence queries are allocated round-robin up front (P2-2) so the
        # event-wide budget is shared fairly, not consumed by early companies.
        for suffix in assigned_queries.get(cand.name, []):
            ev_hits = _search_with_cache(
                cache=cache, cache_enabled=cache_enabled,
                search_provider=search_provider, query=f'"{cand.name}" {suffix}',
                kind="web", count=count_web, lang=lang,
                hits_counter=(lambda hit: None),
                now=now, refresh=refresh,
            )
            _tally(ev_hits, row)
            for hit in ev_hits["results"]:
                if not _is_article_like(hit.url):
                    continue
                if not _evidence_relevant(hit.url, hit.title):
                    continue
                raw_ev.append(
                    EvidenceItem(
                        type=classify_url_type(hit.url),
                        url=hit.url,
                        source_domain=domain_of(hit.url),
                    )
                )
        row.evidence = merge_evidence(raw_ev)

        # raw_extraction → enriched promotion check
        if not row.source_snippet:
            # Shouldn't happen — extraction enforces snippet — but guard anyway.
            row.enrichment_status = "needs_review"
            row.enrichment_warnings.append("missing source_snippet after extraction")

        if row.degraded:
            row.enrichment_warnings.append(
                "search degraded for one or more queries; evidence may be "
                "incomplete (row will re-enrich on the next run)"
            )

        rows.append(row)
        # Append per-company AS SOON AS the row finishes (durability — review r2 #4):
        # a later company's API error never loses an already-completed row.
        resume.append(_to_dict(
            row, input_fp=fp_by_name[cand.name], enriched_at=now.isoformat(),
        ))

    # Surface search degradation (blind review R1#2): a keyless backend that hit
    # rate limits returned empty results for some queries — make that visible in
    # the run summary so "no news/url" isn't silently indistinguishable from a
    # genuine absence of evidence.
    if getattr(search_provider, "degraded", False):
        n = getattr(search_provider, "degraded_queries", 0)
        warnings.append(
            f"search degraded: {n} query(ies) hit rate limits and returned no "
            f"results (provider={provider_sig}); affected companies may "
            "under-report news/official-URL evidence"
        )
    if error_queries:
        warnings.append(
            f"search errors: {error_queries} query(ies) raised and degraded to "
            f"empty (provider={provider_sig}); affected rows will re-enrich on "
            "the next run"
        )

    return EnrichmentResult(
        rows=rows,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        skipped_from_resume=skipped,
        warnings=warnings,
    )


def _search_with_cache(
    *,
    cache: _SearchCache,
    cache_enabled: bool,
    search_provider: SearchProvider,
    query: str,
    kind: str,
    count: int,
    lang: str,
    days: int | None = None,
    hits_counter: Callable[..., object],
    now: datetime,
    refresh: bool = False,
) -> dict:
    """Returns `{results: list[SearchResult], was_hit: bool, degraded: bool}`.
    The cache stores serialized SearchResult dicts (title/url/snippet/source) —
    `extra` and `published_at` are dropped for portability.

    `refresh` skips the cache READ (forcing a live call) but still WRITES the
    fresh result, so a subsequent non-refresh run benefits (review r2 #3).

    Degradation (news plan N1): when the provider reports the call degraded to
    empty (e.g. rate-limit past retries), the empty result is NOT cached — a
    degraded "no results" must not block retries for cache_ttl_days. Genuine
    empty results are still cached so absent companies aren't re-queried.
    """
    if cache_enabled and not refresh:
        cached = cache.get(query, kind=kind, lang=lang, count=count, days=days, now=now)
        if cached is not None:
            return {
                "results": [_dict_to_searchresult(d) for d in cached],
                "was_hit": True,
                "degraded": False,
            }
    try:
        live = search_provider.search(
            query, kind=kind, count=count, days=days, lang=lang
        )
    except MCPError:
        # Config errors etc. stay fatal — preflight ping() is the misconfig gate.
        raise
    except Exception as exc:
        # A mid-run search failure degrades THIS query instead of aborting the
        # whole enrichment stage (N2): the row gets flagged degraded (N1) so the
        # next run retries it; nothing is cached.
        return {
            "results": [],
            "was_hit": False,
            "degraded": True,
            "error": f"{kind} search failed for query {query!r}: "
                     f"{type(exc).__name__}: {exc}",
        }
    degraded = bool(getattr(search_provider, "last_call_degraded", False))
    if cache_enabled and not degraded:
        cache.put(
            query, kind=kind, lang=lang, count=count, days=days,
            results=[_searchresult_to_dict(r) for r in live], now=now,
        )
    return {"results": live, "was_hit": False, "degraded": degraded}
