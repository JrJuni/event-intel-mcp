from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: datetime | None = None
    extra: dict = field(default_factory=dict)


class SearchProvider(ABC):
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


class BraveSearchProvider(SearchProvider):
    """Default SearchProvider using Brave Search API.

    httpx is lightweight and imported at module use, not load.
    """

    BASE_URL = "https://api.search.brave.com/res/v1"

    def __init__(self, *, api_key: str | None = None, timeout: float = 15.0):
        self._api_key = api_key or os.environ.get("BRAVE_API_KEY")
        self.timeout = timeout

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
