from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
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
        (caller degrades to empty + flags, R1#2/N1).
        """
        attempt = 0
        while True:
            _DDGS_RATE_LIMITER.wait(
                self.min_interval_ms / 1000.0, clock=self._clock, sleep=self._sleep
            )
            try:
                return fn()
            except Exception as exc:
                if self._is_no_results(exc):
                    return []
                attempt += 1
                if attempt > self.max_retries:
                    self._degraded_queries += 1
                    self.last_error = repr(exc)
                    return None
                self._sleep(min(2.0**attempt, 15.0))

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

        raw = self._call_with_retry(_do)
        if raw is None:  # rate-limit graceful empty
            self.last_call_degraded = True
            return []
        return [self._to_result(item, kind) for item in raw]

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


def make_search_provider(config: dict) -> SearchProvider:
    """Factory: select the search backend from ``search.provider`` (default ddgs).

    Mirrors ``providers.llm.make_llm_provider``. ddgs is the zero-config default
    (keyless); brave (keyed) and searxng (self-hosted, requires searxng_url) are
    opt-in. Invalid names / missing required config fail loud with CONFIG_ERROR.
    """
    search_cfg = (config or {}).get("search", {}) or {}
    provider = search_cfg.get("provider", "ddgs")
    if provider == "ddgs":
        return DdgsSearchProvider(
            min_interval_ms=int(search_cfg.get("min_interval_ms", 1100)),
            max_retries=int(search_cfg.get("max_retries", 5)),
            backend=str(search_cfg.get("ddgs_backend", "auto") or "auto"),
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
