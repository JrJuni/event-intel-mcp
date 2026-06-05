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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage

if TYPE_CHECKING:
    from event_intel.events.extraction import ExhibitorCandidate
    from event_intel.providers.search import SearchProvider, SearchResult


# Bump when enrichment parsing/filtering semantics change so stale on-disk
# search cache + resume rows are invalidated instead of silently reused.
#   v1 → original.
#   v2 → Brave news parser fix (top-level results) + published_at + non-article
#        news path filter (Phase 18U). Old v1 entries cached empty news.
ENRICH_CACHE_VERSION = 2

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
    extraction_confidence: float = 1.0
    enrichment_status: str = "enriched"   # "enriched" | "needs_review" | "failed"
    enrichment_warnings: list[str] = field(default_factory=list)


@dataclass
class EnrichmentResult:
    rows: list[EnrichedExhibitor]
    cache_hits: int
    cache_misses: int
    skipped_from_resume: int
    warnings: list[str]


# ---------- cache ----------


class _SearchCache:
    """Lightweight on-disk cache. One JSON file per (query, kind, lang) hash."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _key(query: str, kind: str, lang: str) -> str:
        # Version prefix → a parser/semantics bump (ENRICH_CACHE_VERSION) yields
        # new keys, so stale entries (e.g. v1's empty news) are never reused.
        h = hashlib.sha1(
            f"v{ENRICH_CACHE_VERSION}|{kind}|{lang}|{query}".encode("utf-8")
        ).hexdigest()
        return h

    def get(self, query: str, *, kind: str, lang: str) -> list[dict] | None:
        path = self.root / f"{self._key(query, kind, lang)}.json"
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def put(self, query: str, *, kind: str, lang: str, results: list[dict]) -> None:
        path = self.root / f"{self._key(query, kind, lang)}.json"
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False)
        except OSError:
            pass  # cache is best-effort; ignore disk hiccups


# ---------- resume artifact ----------


class _ResumeStore:
    """JSONL — one row per enriched exhibitor, keyed by name. Append-only.

    Reading is fault-tolerant: a half-written line at EOF is ignored.
    """

    def __init__(self, path: Path):
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


def _score_candidate_url(name: str, candidate: "SearchResult", *, threshold: float) -> float:
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
    name: str, web_hits: list["SearchResult"], *, threshold: float
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


def _searchresult_to_dict(r: "SearchResult") -> dict:
    return {
        "title": r.title,
        "url": r.url,
        "snippet": r.snippet,
        "source": r.source,
        "published_at": r.published_at.isoformat() if r.published_at else None,
    }


def _dict_to_searchresult(d: dict) -> "SearchResult":
    from datetime import datetime

    from event_intel.providers.search import SearchResult

    published_at = None
    raw = d.get("published_at")
    if raw:
        try:
            published_at = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            published_at = None
    return SearchResult(
        title=d.get("title", ""),
        url=d.get("url", ""),
        snippet=d.get("snippet", ""),
        source=d.get("source"),
        published_at=published_at,
    )


def _to_dict(row: EnrichedExhibitor) -> dict:
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
        "extraction_confidence": row.extraction_confidence,
        "enrichment_status": row.enrichment_status,
        "enrichment_warnings": row.enrichment_warnings,
        "_cache_version": ENRICH_CACHE_VERSION,
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
        extraction_confidence=float(d.get("extraction_confidence", 1.0)),
        enrichment_status=d.get("enrichment_status", "enriched"),
        enrichment_warnings=list(d.get("enrichment_warnings", [])),
    )


def enrich_exhibitors(
    *,
    candidates: list["ExhibitorCandidate"],
    workspace_id: str,
    lang: str = "en",
    config: dict,
    search_provider: "SearchProvider",
    cache_dir: Path | None = None,
    resume_path: Path | None = None,
    max_companies: int | None = None,
) -> EnrichmentResult:
    """Enrich a list of extracted candidates with official URL + news.

    `cache_dir` defaults to `~/.event-intel/cache/search/{workspace_id}/`.
    `resume_path` defaults to `~/.event-intel/resume/{workspace_id}.jsonl`.
    """
    try:
        enrichment_cfg = config["enrichment"]
        max_default = int(enrichment_cfg["max_companies"])
        count_web = int(enrichment_cfg["brave_count_web"])
        count_news = int(enrichment_cfg["brave_count_news"])
        news_days = int(enrichment_cfg["news_days_back"])
        cache_enabled = bool(enrichment_cfg.get("cache_enabled", True))
        url_threshold = float(enrichment_cfg["official_url_levenshtein_threshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.ENRICHMENT,
            message=f"missing or invalid enrichment config: {exc}",
            hint={"required": [
                "enrichment.max_companies", "enrichment.brave_count_web",
                "enrichment.brave_count_news", "enrichment.news_days_back",
                "enrichment.official_url_levenshtein_threshold",
            ]},
        ) from exc

    home = Path.home() / ".event-intel"
    cache_root = cache_dir or (home / "cache" / "search" / workspace_id)
    resume_file = resume_path or (home / "resume" / f"{workspace_id}.jsonl")
    cache = _SearchCache(cache_root)
    resume = _ResumeStore(resume_file)

    done_by_name = resume.load_done()
    cap = max_companies or max_default
    capped = candidates[:cap]
    warnings: list[str] = []
    if len(candidates) > cap:
        warnings.append(
            f"capped enrichment at {cap}/{len(candidates)} exhibitors "
            "(set enrichment.max_companies in config to raise)"
        )

    cache_hits = 0
    cache_misses = 0
    skipped = 0
    rows: list[EnrichedExhibitor] = []

    for cand in capped:
        if cand.name in done_by_name:
            rows.append(_from_dict(done_by_name[cand.name]))
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
            )
            if web_hits["was_hit"]:
                cache_hits += 1
            else:
                cache_misses += 1
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
        )
        if news_hits["was_hit"]:
            cache_hits += 1
        else:
            cache_misses += 1
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

        # raw_extraction → enriched promotion check
        if not row.source_snippet:
            # Shouldn't happen — extraction enforces snippet — but guard anyway.
            row.enrichment_status = "needs_review"
            row.enrichment_warnings.append("missing source_snippet after extraction")

        rows.append(row)
        resume.append(_to_dict(row))

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
    search_provider: "SearchProvider",
    query: str,
    kind: str,
    count: int,
    lang: str,
    days: int | None = None,
    hits_counter,
) -> dict:
    """Returns `{results: list[SearchResult], was_hit: bool}`. The cache stores
    serialized SearchResult dicts (title/url/snippet/source) — `extra` and
    `published_at` are dropped for portability."""
    if cache_enabled:
        cached = cache.get(query, kind=kind, lang=lang)
        if cached is not None:
            return {
                "results": [_dict_to_searchresult(d) for d in cached],
                "was_hit": True,
            }
    try:
        live = search_provider.search(
            query, kind=kind, count=count, days=days, lang=lang
        )
    except Exception as exc:
        raise MCPError(
            error_code=ErrorCode.UPSTREAM_ERROR,
            stage=Stage.ENRICHMENT,
            message=f"Brave {kind} search failed for query {query!r}: {exc}",
            hint={"query": query, "kind": kind},
            retryable=True,
        ) from exc
    if cache_enabled:
        cache.put(
            query, kind=kind, lang=lang,
            results=[_searchresult_to_dict(r) for r in live],
        )
    return {"results": live, "was_hit": False}
