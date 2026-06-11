from __future__ import annotations

from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass


@dataclass
class FetchResult:
    url: str
    body: str | None
    status_code: int | None
    error: str | None = None


class FetchProvider(ABC):
    @abstractmethod
    def fetch(self, url: str) -> FetchResult: ...

    @abstractmethod
    def fetch_many(
        self, urls: list[str], *, max_workers: int = 5
    ) -> list[FetchResult]: ...


class HttpxTrafilaturaFetchProvider(FetchProvider):
    """Default FetchProvider using httpx + trafilatura.

    trafilatura is imported lazily inside extract; httpx at call time.
    """

    def __init__(self, *, timeout: float = 10.0, user_agent: str = "event-intel-mcp/0.1") -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def fetch(self, url: str) -> FetchResult:
        import httpx

        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            ) as client:
                resp = client.get(url)
                if resp.status_code >= 400:
                    return FetchResult(
                        url=url, body=None, status_code=resp.status_code, error=f"HTTP {resp.status_code}"
                    )
                from event_intel.textenc import decode_html

                # Header charset > meta sniff > utf-8 (not resp.text, which
                # can't distinguish a declared charset from httpx's default).
                html = decode_html(resp.content, header_charset=resp.charset_encoding)
                body = self._extract(html)
                return FetchResult(url=url, body=body, status_code=resp.status_code)
        except Exception as e:
            return FetchResult(url=url, body=None, status_code=None, error=str(e))

    @staticmethod
    def _extract(html: str) -> str | None:
        import trafilatura

        return trafilatura.extract(html, include_comments=False, include_tables=False)

    def fetch_many(
        self, urls: list[str], *, max_workers: int = 5
    ) -> list[FetchResult]:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            return list(pool.map(self.fetch, urls))
