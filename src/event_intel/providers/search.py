from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from event_intel.errors import ErrorCode, MCPError, Stage

# Search backends selectable via `search.provider` (zero-config plan). searxng
# lands in a later slice; the factory builds brave + ddgs.
_VALID_SEARCH_PROVIDERS: tuple[str, ...] = ("ddgs", "searxng", "brave")


class _RateLimiter:
    """Process-wide, thread-safe minimum-interval gate (blind review R1#4).

    A module-level singleton so it survives provider re-creation (a fresh
    DdgsSearchProvider is built per event build) and serializes across FastMCP
    worker threads — a provider-local limiter would not bound the real request
    rate. ``clock``/``sleep`` are injectable for deterministic tests.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(
        self,
        min_interval_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval_s <= 0:
            return
        with self._lock:
            gap = min_interval_s - (clock() - self._last)
            if gap > 0:
                sleep(gap)
            self._last = clock()


# Single shared limiter for all ddgs calls in this process.
_DDGS_RATE_LIMITER = _RateLimiter()


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: datetime | None = None
    extra: dict = field(default_factory=dict)


class SearchProvider(ABC):
    """Search backend contract.

    Degradation convention (N1): a provider that degrades a query to an EMPTY
    result instead of raising (e.g. ddgs after retry exhaustion) SHOULD expose
    ``last_call_degraded: bool`` — True iff the most recent ``search()`` call on
    this instance returned empty *because of* degradation rather than a genuine
    absence of results. The flag is only valid until the next ``search()`` call
    (instances are per-build and builds are single-threaded). Consumers read it
    via ``getattr(provider, "last_call_degraded", False)`` so providers and test
    fakes without the attribute keep working unchanged.
    """

    @abstractmethod
    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]: ...

    @abstractmethod
    def ping(self) -> dict: ...

    @property
    def cache_signature(self) -> str:
        """Stable string distinguishing this backend's result space, used in the
        enrichment cache key + resume fingerprint. Include anything that changes
        WHAT results come back (provider, package version, region/recency mapping)
        so switching providers never reuses another backend's cached answers.
        """
        return self.__class__.__name__


class BraveSearchProvider(SearchProvider):
    """Default SearchProvider using Brave Search API.

    httpx is lightweight and imported at module use, not load.
    """

    BASE_URL = "https://api.search.brave.com/res/v1"

    def __init__(self, *, api_key: str | None = None, timeout: float = 15.0) -> None:
        self._api_key = api_key or os.environ.get("BRAVE_API_KEY")
        self.timeout = timeout

    @property
    def cache_signature(self) -> str:
        return "brave/v1"

    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]:
        if not self._api_key:
            raise RuntimeError("BRAVE_API_KEY not set")
        import httpx

        endpoint = f"{self.BASE_URL}/{kind}/search"
        params: dict = {"q": query, "count": count, "search_lang": lang}
        if days is not None and kind == "news":
            params["freshness"] = self._freshness(days)
        headers = {
            "X-Subscription-Token": self._api_key,
            "Accept": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(endpoint, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return self._parse(data, kind)

    @staticmethod
    def _freshness(days: int) -> str:
        if days <= 1:
            return "pd"
        if days <= 7:
            return "pw"
        if days <= 31:
            return "pm"
        return "py"

    @staticmethod
    def _parse(data: dict, kind: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        # Brave response shapes differ by endpoint:
        #   /web/search  → {"web": {"results": [...]}}
        #   /news/search → {"type": "news", "results": [...]}   (top-level, NOT nested)
        # Reading data["news"]["results"] for news silently yields [] → news_count
        # always 0 → evidence_floor never reaches 2 → S tier unreachable. (bug fixed 2026-06-05)
        if kind == "news":
            bucket = data.get("results", [])
        else:
            bucket = data.get("web", {}).get("results", [])
        for item in bucket:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", "") or item.get("snippet", ""),
                    source=item.get("source") or item.get("meta_url", {}).get("hostname"),
                    published_at=BraveSearchProvider._parse_published(item),
                    extra={k: v for k, v in item.items() if k not in {"title", "url"}},
                )
            )
        return results

    @staticmethod
    def _parse_published(item: dict) -> datetime | None:
        """Best-effort published timestamp from a Brave news item (`page_age`/`age`).

        Returns None on any parse failure — published_at is advisory; the evidence
        floor only cares whether news exists, not exactly when.
        """
        from event_intel.timeutil import parse_iso_utc

        raw = item.get("page_age") or item.get("age")
        # parse_iso_utc normalizes to an aware UTC datetime (or None) so a
        # date-only / tz-less Brave timestamp never collides with the UTC-aware
        # reference_date in recency scoring (review round-2 #1).
        return parse_iso_utc(raw if isinstance(raw, str) else None)

    def ping(self) -> dict:
        """Lightweight health check. Returns quota if header is present, else null."""
        if not self._api_key:
            return {"status": "missing_key", "remaining_quota": None}
        try:
            import httpx

            headers = {
                "X-Subscription-Token": self._api_key,
                "Accept": "application/json",
            }
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    f"{self.BASE_URL}/web/search",
                    params={"q": "ping", "count": 1},
                    headers=headers,
                )
                resp.raise_for_status()
                quota_header = resp.headers.get("X-RateLimit-Remaining")
                quota = int(quota_header) if quota_header and quota_header.isdigit() else None
                return {"status": "ok", "remaining_quota": quota}
        except Exception as e:
            return {"status": "error", "remaining_quota": None, "error": str(e)}


class DdgsSearchProvider(SearchProvider):
    """Keyless zero-config search via the ``ddgs`` aggregator (multiple engines).

    Unofficial / best-effort. ddgs raises for EVERYTHING — including a query
    that simply has no results — so failures are classified (N2):
      - ``DDGSException("No results found.")`` → genuine empty: returned as []
        immediately, NOT degraded, cacheable.
      - everything else (rate-limit, timeout, transport) → exponential backoff
        up to ``max_retries``; past that the query degrades to an EMPTY result
        (``degraded`` + per-call ``last_call_degraded`` + ``last_error``)
        instead of raising — a build lowers a company's tier rather than
        aborting (blind review R1#2 / news plan N2).
    Note: ``ddgs.exceptions.RatelimitException`` is never raised by ddgs 9.14.x
    (rate limits surface as generic DDGSException), hence the broad except.

    ``backend`` maps to ddgs' engine selection ("auto" shuffles all engines per
    call — news lane: duckduckgo/bing/yahoo; a comma-list pins specific ones).
    A fresh DDGS() client per attempt means each retry re-shuffles engines, so
    retrying IS backend rotation. Caveat: the "auto" text lane tries
    wikipedia/grokipedia first (highest priority) which return little for
    '"{name}" official site' queries — harmless, the aggregator continues.
    ``ddgs`` is imported lazily (cold-start safe).
    """

    # Our lang contract (en/ko/ja/...) → ddgs region. Unknown → worldwide (wt-wt).
    _REGION = {"en": "us-en", "ko": "kr-kr", "ja": "jp-jp", "zh": "zh-cn"}

    def __init__(
        self,
        *,
        min_interval_ms: int = 1100,
        max_retries: int = 5,
        backend: str = "auto",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_ms = int(min_interval_ms)
        # Provisional ceiling (N2) — to be finalized from R2 smoke failure data (R3).
        self.max_retries = int(max_retries)
        self.backend = str(backend or "auto")
        self._clock = clock
        self._sleep = sleep
        self._degraded_queries = 0
        # Per-call flag (N1): True iff the LAST search() degraded to empty.
        # Valid only until the next search() on this instance.
        self.last_call_degraded = False
        # repr() of the last exception that exhausted retries (diagnostics, R1).
        self.last_error: str | None = None
        # Failure-pattern events, one per live search() (R1). The enrichment
        # layer drains these into the diagnostics JSONL via drain_events().
        self.events: list[dict] = []
        self._last_meta: dict = {}

    @property
    def degraded(self) -> bool:
        return self._degraded_queries > 0

    @property
    def degraded_queries(self) -> int:
        return self._degraded_queries

    @property
    def cache_signature(self) -> str:
        # Package version + region-map revision: a ddgs upgrade or mapping change
        # invalidates cross-version cache (R1#1 verification upgrade).
        from importlib.metadata import PackageNotFoundError, version

        try:
            v = version("ddgs")
        except PackageNotFoundError:
            v = "unknown"
        # A CONFIGURED backend deterministically changes the result space → part
        # of the key. auto's per-call shuffle is nondeterministic by design; the
        # cache stores "an acceptable answer for this query" (N2).
        return f"ddgs/{v}/region1/b={self.backend}"

    @staticmethod
    def _timelimit(days: int) -> str:
        if days <= 1:
            return "d"
        if days <= 7:
            return "w"
        if days <= 31:
            return "m"
        return "y"

    def _region(self, lang: str) -> str:
        return self._REGION.get((lang or "").lower(), "wt-wt")

    @staticmethod
    def _is_no_results(exc: Exception) -> bool:
        """A query that genuinely has no results — ddgs raises instead of
        returning []. String-matches a ddgs-internal message; pyproject pins
        ``ddgs<10`` and a literal-pin test fails loud if an upgrade changes it.
        """
        from ddgs.exceptions import DDGSException

        return isinstance(exc, DDGSException) and str(exc).startswith("No results found")

    def _call_with_retry(self, fn: Callable[[], list]) -> list | None:
        """Throttle + classify + exponential backoff (N2). Returns fn()'s result,
        [] for a genuine no-results answer, or None when retries are exhausted
        (caller degrades to empty + flags, R1#2/N1). Records attempt metadata in
        ``self._last_meta`` for the R1 failure-pattern event.
        """
        excs: list[str] = []
        while True:
            _DDGS_RATE_LIMITER.wait(
                self.min_interval_ms / 1000.0, clock=self._clock, sleep=self._sleep
            )
            try:
                result = fn()
            except Exception as exc:
                if self._is_no_results(exc):
                    self._last_meta = {
                        "attempts": len(excs) + 1, "exc_classes": excs,
                        "outcome": "no_results",
                    }
                    return []
                excs.append(type(exc).__name__)
                if len(excs) > self.max_retries:
                    self._degraded_queries += 1
                    self.last_error = repr(exc)
                    self._last_meta = {
                        "attempts": len(excs), "exc_classes": excs,
                        "outcome": "degraded",
                    }
                    return None
                self._sleep(min(2.0 ** len(excs), 15.0))
            else:
                self._last_meta = {
                    "attempts": len(excs) + 1, "exc_classes": excs,
                    "outcome": "recovered" if excs else "ok",
                }
                return result

    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]:
        from ddgs import DDGS

        self.last_call_degraded = False
        region = self._region(lang)
        timelimit = self._timelimit(days) if days is not None else None

        def _do() -> list:
            client = DDGS()
            if kind == "news":
                return client.news(
                    query, region=region, timelimit=timelimit, max_results=count,
                    backend=self.backend,
                )
            return client.text(
                query, region=region, timelimit=timelimit, max_results=count,
                backend=self.backend,
            )

        start = self._clock()
        raw = self._call_with_retry(_do)
        self.events.append({
            "ts": datetime.now(UTC).isoformat(),
            "provider": "ddgs",
            "backend": self.backend,
            "kind": kind,
            "lang": lang,
            "query": query,
            "elapsed_s": round(self._clock() - start, 3),
            **self._last_meta,
        })
        if raw is None:  # rate-limit graceful empty
            self.last_call_degraded = True
            return []
        return [self._to_result(item, kind) for item in raw]

    def drain_events(self) -> list[dict]:
        """Return + clear accumulated failure-pattern events (R1). The consumer
        (enrichment) writes them to the diagnostics JSONL.
        """
        events, self.events = self.events, []
        return events

    @staticmethod
    def _to_result(item: dict, kind: str) -> SearchResult:
        from event_intel.timeutil import parse_iso_utc

        date_raw = item.get("date")
        return SearchResult(
            title=item.get("title", "") or "",
            url=item.get("href") or item.get("url") or "",
            snippet=item.get("body", "") or "",
            source=item.get("source") or None,
            published_at=parse_iso_utc(date_raw) if isinstance(date_raw, str) else None,
            extra={k: v for k, v in item.items() if k not in {"title", "href", "url"}},
        )

    def ping(self) -> dict:
        # No live query — DDG rate-limits aggressively, so preflight must not burn
        # the budget. Keyless best-effort (R1#6): never overstated as "ok".
        return {"status": "best_effort", "provider": "ddgs", "remaining_quota": None}


class SearxngSearchProvider(SearchProvider):
    """Search via a self-hosted SearXNG instance's JSON API (keyless).

    A reliability lane between brave (hosted, keyed) and ddgs (keyless, fragile).
    Requires a reachable instance with the JSON output format enabled — when it is
    NOT, SearXNG answers 403 / non-JSON; ping() surfaces that as a config problem
    (blind review R1#5) so preflight fails with a clear fix rather than mid-build.
    Parsing is tolerant of instance-to-instance field variance. httpx is lazy.
    """

    def __init__(self, *, base_url: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def cache_signature(self) -> str:
        return "searxng/v1"

    @staticmethod
    def _time_range(days: int) -> str:
        if days <= 1:
            return "day"
        if days <= 7:
            return "week"
        if days <= 31:
            return "month"
        return "year"

    def _params(self, query: str, *, kind: str, days: int | None, lang: str) -> dict:
        params: dict = {
            "q": query,
            "format": "json",
            "categories": "news" if kind == "news" else "general",
            "pageno": 1,
        }
        if lang:
            params["language"] = lang
        if days is not None:
            params["time_range"] = self._time_range(days)
        return params

    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]:
        import httpx

        params = self._params(query, kind=kind, days=days, lang=lang)
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.get(f"{self.base_url}/search", params=params)
        if resp.status_code == 403:
            # JSON format disabled on the instance — a config issue, not transient.
            raise RuntimeError(
                "SearXNG returned 403 — enable the 'json' output format on the instance"
            )
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — non-JSON => format not enabled
            raise RuntimeError(
                "SearXNG returned non-JSON — is the 'json' output format enabled?"
            ) from exc
        results = (data.get("results") or [])[:count]
        return [self._to_result(item) for item in results]

    @staticmethod
    def _to_result(item: dict) -> SearchResult:
        from event_intel.timeutil import parse_iso_utc

        pub = item.get("publishedDate")
        return SearchResult(
            title=item.get("title", "") or "",
            url=item.get("url", "") or "",
            snippet=item.get("content", "") or "",
            source=item.get("engine") or None,
            published_at=parse_iso_utc(pub) if isinstance(pub, str) else None,
            extra={k: v for k, v in item.items() if k not in {"title", "url"}},
        )

    def ping(self) -> dict:
        if not self.base_url:
            return {
                "status": "missing_config",
                "message": "search.searxng_url is not set",
                "fix": "Set search.searxng_url to your SearXNG instance URL",
            }
        try:
            import httpx

            with httpx.Client(timeout=5.0) as client:
                resp = client.get(
                    f"{self.base_url}/search",
                    params={"q": "ping", "format": "json", "pageno": 1},
                )
            if resp.status_code == 403:
                return {
                    "status": "missing_config",
                    "message": "SearXNG json format not enabled (403)",
                    "fix": "Enable `formats: [json]` in the SearXNG settings.yml",
                }
            resp.raise_for_status()
            resp.json()  # confirm JSON
            return {"status": "ok", "remaining_quota": None}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "remaining_quota": None, "error": str(e)}


class GoogleNewsRssSearchProvider(SearchProvider):
    """Keyless NEWS-ONLY lane via the public Google News RSS search feed (N3).

    Used as the fallback half of ``FallbackSearchProvider`` when the primary
    (ddgs) degrades a news query. ``kind="web"`` always returns [] — this lane
    is never composed for web. All failures degrade to empty (never raise) and
    set ``last_call_degraded``. A modest courtesy interval guards the feed
    (separate from the ddgs limiter). ToS note: public RSS endpoint, robots-
    permitted path, low volume (fires only on degraded news queries), no
    article fetching here — bodies go through the B1 robots-gated lane, whose
    redirect-follow resolves the ``news.google.com/rss/articles/...`` wrapper
    links this feed returns. Known limitation: ``domain_of()`` on the wrapper
    URL is news.google.com, so ``same_site`` checks never match (the
    title/snippet token gate still applies).

    httpx / xml.etree / email.utils are imported lazily (cold-start safe).
    """

    BASE_URL = "https://news.google.com/rss/search"
    # lang → (hl, gl) for the feed's locale params.
    _LOCALE = {"en": ("en-US", "US"), "ko": ("ko", "KR"),
               "ja": ("ja", "JP"), "zh": ("zh-CN", "CN")}

    def __init__(
        self,
        *,
        min_interval_ms: int = 500,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        transport: object | None = None,
    ) -> None:
        self.min_interval_ms = int(min_interval_ms)
        self.timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self._transport = transport  # httpx transport override (tests)
        self._degraded_queries = 0
        self.last_call_degraded = False
        self.events: list[dict] = []

    @property
    def degraded(self) -> bool:
        return self._degraded_queries > 0

    @property
    def degraded_queries(self) -> int:
        return self._degraded_queries

    @property
    def cache_signature(self) -> str:
        return "gnrss/v1"

    def ping(self) -> dict:
        # No live call — keyless best-effort fallback lane.
        return {"status": "best_effort", "provider": "google_news_rss",
                "remaining_quota": None}

    def drain_events(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]:
        self.last_call_degraded = False
        if kind != "news":
            return []  # news-only lane by contract
        start = self._clock()
        outcome = "ok"
        exc_class: str | None = None
        try:
            results = self._fetch_feed(query, count=count, days=days, lang=lang)
        except Exception as exc:
            self._degraded_queries += 1
            self.last_call_degraded = True
            outcome, exc_class = "degraded", type(exc).__name__
            results = []
        self.events.append({
            "ts": datetime.now(UTC).isoformat(),
            "provider": "google_news_rss",
            "backend": "rss",
            "kind": kind,
            "lang": lang,
            "query": query,
            "attempts": 1,
            "exc_classes": [exc_class] if exc_class else [],
            "outcome": outcome,
            "elapsed_s": round(self._clock() - start, 3),
        })
        return results

    def _fetch_feed(
        self, query: str, *, count: int, days: int | None, lang: str
    ) -> list[SearchResult]:
        import httpx

        q = query if days is None else f"{query} when:{days}d"
        hl, gl = self._LOCALE.get((lang or "").lower(), ("en-US", "US"))
        _GNRSS_RATE_LIMITER.wait(
            self.min_interval_ms / 1000.0, clock=self._clock, sleep=self._sleep
        )
        kwargs: dict = {"timeout": self.timeout, "follow_redirects": True}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        with httpx.Client(**kwargs) as client:
            resp = client.get(
                self.BASE_URL,
                params={"q": q, "hl": hl, "gl": gl, "ceid": f"{gl}:{hl}"},
            )
            resp.raise_for_status()
            text = resp.text
        return self._parse_rss(text, count=count)

    @staticmethod
    def _parse_rss(xml_text: str, *, count: int) -> list[SearchResult]:
        import html as _html
        import re as _re
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime

        root = ET.fromstring(xml_text)
        out: list[SearchResult] = []
        for item in root.iter("item"):
            if len(out) >= count:
                break
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not title or not link:
                continue
            desc_raw = item.findtext("description") or ""
            snippet = _re.sub(r"<[^>]+>", " ", _html.unescape(desc_raw))
            snippet = " ".join(snippet.split())
            published = None
            pub_raw = item.findtext("pubDate")
            if pub_raw:
                try:
                    published = parsedate_to_datetime(pub_raw)
                except (TypeError, ValueError):
                    published = None
            out.append(SearchResult(
                title=title,
                url=link,
                snippet=snippet,
                source=(item.findtext("source") or "").strip() or None,
                published_at=published,
            ))
        return out


# Courtesy limiter for the RSS feed — separate from the ddgs limiter so the two
# lanes don't serialize each other.
_GNRSS_RATE_LIMITER = _RateLimiter()


class FallbackSearchProvider(SearchProvider):
    """Compose a primary lane with a keyless fallback (N3) + supplement (#15-1).

    Two firing modes, news-kind only:
    - **Fallback** (N3): primary reports the call DEGRADED → the fallback's
      answer replaces it. Genuine empties do NOT fall back — they are real
      answers (budget + determinism).
    - **Supplement** (cn20 re-measure finding: ddgs news SUPPLY tops out at
      3–6 articles for most companies, far below the criterion-⑤ bar): when
      the primary answers but returns FEWER than min(supplement_min, count)
      results, the fallback is queried too and its non-duplicate items
      (canonical-URL dedupe — best-effort: Google News wrapper URLs never
      match publisher URLs, the B2 body near-dup pass is the real guard) are
      APPENDED up to ``count``. A supplemented answer is a real answer —
      never marked degraded. ``supplement_min=0`` disables.

    The combined ``cache_signature`` (incl. the supplement threshold — it
    changes the result space) differs from the bare primary's, so toggling
    either mode never replays the other mode's cache and re-fingerprints
    resume rows once (same mechanism as a provider switch).
    """

    def __init__(
        self,
        primary: SearchProvider,
        fallback: SearchProvider,
        *,
        kinds: tuple[str, ...] = ("news",),
        supplement_min: int = 0,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.kinds = tuple(kinds)
        self.supplement_min = int(supplement_min)
        self.last_call_degraded = False

    @property
    def cache_signature(self) -> str:
        sig = f"{self.primary.cache_signature}+fb={self.fallback.cache_signature}"
        if self.supplement_min:
            sig += f"/sup{self.supplement_min}"
        return sig

    @property
    def degraded(self) -> bool:
        return bool(
            getattr(self.primary, "degraded", False)
            or getattr(self.fallback, "degraded", False)
        )

    @property
    def degraded_queries(self) -> int:
        return int(getattr(self.primary, "degraded_queries", 0)) + int(
            getattr(self.fallback, "degraded_queries", 0)
        )

    def ping(self) -> dict:
        status = dict(self.primary.ping())
        status["news_fallback"] = getattr(
            self.fallback, "cache_signature", self.fallback.__class__.__name__
        )
        return status

    def drain_events(self) -> list[dict]:
        events: list[dict] = []
        for p in (self.primary, self.fallback):
            drain = getattr(p, "drain_events", None)
            if callable(drain):
                events.extend(drain())
        return events

    def search(
        self,
        query: str,
        *,
        kind: Literal["web", "news"] = "web",
        count: int = 10,
        days: int | None = None,
        lang: str = "en",
    ) -> list[SearchResult]:
        results = self.primary.search(
            query, kind=kind, count=count, days=days, lang=lang
        )
        primary_degraded = bool(getattr(self.primary, "last_call_degraded", False))
        self.last_call_degraded = primary_degraded
        if kind not in self.kinds:
            return results
        if primary_degraded:
            rescued = self.fallback.search(
                query, kind=kind, count=count, days=days, lang=lang
            )
            # Degraded only if BOTH lanes failed to answer; a fallback answer
            # (even an empty feed) is cacheable like any real answer ONLY when
            # the fallback itself didn't degrade.
            self.last_call_degraded = bool(
                getattr(self.fallback, "last_call_degraded", False)
            )
            return rescued
        # Supplement: a healthy-but-thin news answer gets topped up (#15-1).
        if self.supplement_min and len(results) < min(self.supplement_min, count):
            extra = self.fallback.search(
                query, kind=kind, count=count, days=days, lang=lang
            )
            from event_intel.events.evidence import canonical_url

            seen = {canonical_url(r.url) for r in results if r.url}
            merged = list(results)
            for r in extra:
                if len(merged) >= count:
                    break
                if not r.url:
                    continue
                cu = canonical_url(r.url)
                if cu in seen:
                    continue
                seen.add(cu)
                merged.append(r)
            return merged
        return results


def make_search_provider(config: dict) -> SearchProvider:
    """Factory: select the search backend from ``search.provider`` (default ddgs).

    Mirrors ``providers.llm.make_llm_provider``. ddgs is the zero-config default
    (keyless); brave (keyed) and searxng (self-hosted, requires searxng_url) are
    opt-in. Invalid names / missing required config fail loud with CONFIG_ERROR.

    ddgs additionally gets a keyless NEWS fallback lane (``search.news_fallback``,
    default google_news_rss) — brave/searxng raise instead of degrading, so
    wrapping them would never fire and only churn their cache signatures (N3).
    """
    search_cfg = (config or {}).get("search", {}) or {}
    provider = search_cfg.get("provider", "ddgs")
    if provider == "ddgs":
        ddgs = DdgsSearchProvider(
            min_interval_ms=int(search_cfg.get("min_interval_ms", 1100)),
            max_retries=int(search_cfg.get("max_retries", 5)),
            backend=str(search_cfg.get("ddgs_backend", "auto") or "auto"),
        )
        fallback = search_cfg.get("news_fallback", "google_news_rss") or "none"
        if fallback == "none":
            return ddgs
        if fallback == "google_news_rss":
            return FallbackSearchProvider(
                ddgs, GoogleNewsRssSearchProvider(),
                supplement_min=int(search_cfg.get("news_supplement_min", 10)),
            )
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.PREFLIGHT,
            message=f"invalid search.news_fallback: {fallback!r}",
            hint={"allowed": ["google_news_rss", "none"]},
            retryable=False,
        )
    if provider == "brave":
        return BraveSearchProvider()
    if provider == "searxng":
        url = search_cfg.get("searxng_url") or ""
        if not url:
            raise MCPError(
                error_code=ErrorCode.CONFIG_ERROR,
                stage=Stage.PREFLIGHT,
                message="search.searxng_url is required when provider=searxng",
                hint={"fix": "Set search.searxng_url to your SearXNG instance URL"},
                retryable=False,
            )
        return SearxngSearchProvider(base_url=url)
    raise MCPError(
        error_code=ErrorCode.CONFIG_ERROR,
        stage=Stage.PREFLIGHT,
        message=f"invalid search.provider: {provider!r}",
        hint={
            "allowed": list(_VALID_SEARCH_PROVIDERS),
            "fix": "Set search.provider to one of: ddgs, searxng, brave",
        },
        retryable=False,
    )
