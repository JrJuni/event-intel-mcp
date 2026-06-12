"""#16 S4 — homepage-crawl evidence lane (news-replacement experiment).

User decision 2026-06-11: instead of news SEARCH (~150 queries/run, June =
ddgs-only quota), activity evidence comes from the company's OWN website —
its /news /press /newsroom listing pages. This module is the crawler; the
enrichment wiring (``enrichment.evidence_source: homepage``) lands in S5.

Per company with a known official URL:
    1. robots gate (``acquisition.robots`` — never ``urllib.robotparser.read()``,
       playbook #12) → streaming GET of the homepage with a hard byte cap →
       trafilatura body. 200 + body >= min_body_chars and not parked →
       ``official_url`` identity evidence + a fit-input excerpt.
    2. Discover same-registrable-domain press/news links in the homepage HTML
       (evidence._PRESS_RE on the resolved path — same pattern classify_url_type
       uses, so "what counts as a press path" has one definition).
    3. Fetch up to ``max_subpages`` of them (list pages only, static GET).
       200 + body >= min_body_chars → ``press_page`` ACTIVITY evidence
       (type assigned directly — never via classify_url_type).

Degrade ladder: robots deny / 4xx / timeout / thin / parked → that page simply
yields no evidence (warning), mirroring "news search returned 0" in the legacy
lane. ``crawl`` never raises.

Caching mirrors news_body: URL-keyed JSON under the given cache_dir, TTL via
enrichment's ``_is_fresh`` contract. Deterministic verdicts (ok / too_short /
parked / 4xx refusal) are cached; transient failures (429/5xx/transport,
robots deny) are NOT (N1 non-stick). Retry policy is the data-derived R3 rule:
ONE retry for transient shapes only (retry-playbook §2).

All constants PROVISIONAL (offline-only phase — live validation deferred).
Heavy imports (httpx / trafilatura / robots) stay inside method bodies;
``fetch_fn`` is injectable so tests never touch the network.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from event_intel.events import evidence as _evidence
from event_intel.runtime.failure_log import FailureLog

_log = logging.getLogger(__name__)

# Parked / for-sale landers pass the byte gate but are not the company's site.
# Minimal phrase blacklist — a verbose parked page slipping through costs one
# wrong identity grant, the same exposure the legacy official-URL search had.
_PARKED_RE = re.compile(
    r"(domain (?:is )?for sale|buy this domain|parked (?:free )?(?:domain|page)|"
    r"domain parking|this domain has expired|godaddy\.com/domainsearch)",
    re.I,
)

# Fragments are NOT excluded here — urlsplit/canonical_url drop them later;
# excluding '#' in the char class would silently drop "/news#top" entirely.
_HREF_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"']+)["']""", re.I)


@dataclass
class HomepageCrawlConfig:
    enabled: bool = True
    max_subpages: int = 3
    max_bytes_per_page: int = 1_048_576
    timeout_s: float = 10.0
    min_body_chars: int = 200
    cache_ttl_days: int | None = 14
    excerpt_max_chars: int = 2000
    # R3 (retry-playbook §2): ONE retry for transient shapes (429/5xx/transport)
    # only; 4xx refusals are deterministic — never retried.
    max_retries: int = 1
    retry_pause_s: float = 2.0

    @classmethod
    def from_dict(cls, d: dict) -> HomepageCrawlConfig:
        ttl = d.get("cache_ttl_days", 14)
        return cls(
            enabled=bool(d.get("enabled", True)),
            max_subpages=int(d.get("max_subpages", 3)),
            max_bytes_per_page=int(d.get("max_bytes_per_page", 1_048_576)),
            timeout_s=float(d.get("timeout_s", 10.0)),
            min_body_chars=int(d.get("min_body_chars", 200)),
            cache_ttl_days=int(ttl) if ttl is not None else None,
            excerpt_max_chars=int(d.get("excerpt_max_chars", 2000)),
            max_retries=int(d.get("max_retries", 1)),
            retry_pause_s=float(d.get("retry_pause_s", 2.0)),
        )


@dataclass
class HomepageCrawlResult:
    evidence: list[_evidence.EvidenceItem] = field(default_factory=list)
    excerpt: str | None = None          # homepage body head — llm_fit input (S5)
    pages_fetched: int = 0              # live fetches this crawl (cache hits excluded)
    warnings: list[str] = field(default_factory=list)


class HomepageCrawler:
    """Crawl one company's homepage (+ press subpages) into typed evidence.

    ``fetch_fn(url) -> {"status", "text", "final_url", "error"?}`` is injectable
    for tests; the default performs a live streaming GET (news_body pattern).
    ``crawl`` is guaranteed never to raise.
    """

    def __init__(
        self,
        *,
        cfg: HomepageCrawlConfig,
        cache_dir: Path,
        now: datetime,
        fetch_fn: Callable[[str], dict] | None = None,
        transport: object | None = None,
        sleep: Callable[[float], None] | None = None,
        failure_log: FailureLog | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.now = now
        self._fetch_fn = fetch_fn or self._fetch_live
        self._transport = transport  # httpx transport override (tests)
        self.failure_log = failure_log
        self.pages_fetched = 0
        import time as _time

        self._sleep = sleep or _time.sleep  # injectable (tests)

    # ---------- public API ----------

    def crawl(self, official_url: str) -> HomepageCrawlResult:
        """Homepage → identity evidence + excerpt; press subpages → activity."""
        result = HomepageCrawlResult()
        self.pages_fetched = 0
        try:
            self._crawl_inner(official_url, result)
        except Exception as exc:  # belt-and-braces: the lane never raises
            _log.warning("homepage crawl failed for %s: %s", official_url, exc)
            result.warnings.append(
                f"homepage: crawl error for {official_url} ({type(exc).__name__})"
            )
        result.pages_fetched = self.pages_fetched
        return result

    # ---------- crawl flow ----------

    def _crawl_inner(self, official_url: str, result: HomepageCrawlResult) -> None:
        home = self._get_page(official_url, kind="homepage")
        if home is None:
            result.warnings.append(
                f"homepage: {official_url} unreachable/denied — no homepage evidence"
            )
            return

        outcome = home.get("outcome")
        if outcome == "ok":
            body = home.get("body") or ""
            result.evidence.append(
                _evidence.EvidenceItem(
                    type=_evidence.OFFICIAL_URL,
                    url=official_url,
                    source_domain=_evidence.domain_of(official_url),
                )
            )
            result.excerpt = body[: self.cfg.excerpt_max_chars]
        elif outcome == "parked":
            result.warnings.append(
                f"homepage: {official_url} looks like a parked domain — identity not granted"
            )
        else:  # too_short / refused (incl. cached negative verdicts)
            result.warnings.append(
                f"homepage: {official_url} {outcome} — identity not granted"
            )

        press_links = home.get("press_links") or []
        for link in press_links[: self.cfg.max_subpages]:
            page = self._get_page(link, kind="press_page")
            if page is None or page.get("outcome") != "ok":
                result.warnings.append(
                    f"homepage: press page {link} thin/unreachable — no activity evidence"
                )
                continue
            result.evidence.append(
                _evidence.EvidenceItem(
                    type=_evidence.PRESS_PAGE,
                    url=link,
                    source_domain=_evidence.domain_of(link),
                )
            )
        if len(press_links) > self.cfg.max_subpages:
            result.warnings.append(
                f"homepage: {len(press_links)} press links found, fetching first "
                f"{self.cfg.max_subpages} (max_subpages cap)"
            )

    # ---------- page fetch (cache → robots → GET → extract) ----------

    def _get_page(self, url: str, *, kind: str) -> dict | None:
        """Cached-or-fetched page verdict. None = transient failure / robots
        deny (NOT cached). Dict outcomes: ok / too_short / parked / refused —
        all deterministic, all cached.
        """
        cached = self._cache_get(url)
        if cached is not None:
            return cached

        from event_intel.acquisition import robots as _robots
        from event_intel.acquisition.raw_fetch import get_user_agent

        if not _robots.is_allowed(url, user_agent=get_user_agent()):
            self._log(url, kind=kind, outcome="robots_denied")
            return None

        attempts = 0
        while True:
            attempts += 1
            try:
                fetched = self._fetch_fn(url)
            except Exception as exc:  # injected fetch_fn may raise; live one doesn't
                fetched = {"status": None, "text": None, "error": f"{type(exc).__name__}: {exc}"}
            if not fetched.get("error") and fetched.get("text"):
                break
            status = fetched.get("status")
            transient = status is None or status == 429 or (
                isinstance(status, int) and 500 <= status < 600
            )
            if transient and attempts <= self.cfg.max_retries:
                self._sleep(self.cfg.retry_pause_s)
                continue
            self._log(url, kind=kind, outcome="error", status=status,
                      exc=fetched.get("error"), attempts=attempts)
            if not transient:
                # Deterministic 4xx refusal — cache the negative verdict so the
                # page isn't re-fetched every run.
                payload = {"url": url, "body": None, "outcome": "refused",
                           "status": status}
                self._cache_put(url, payload)
                return payload
            return None

        self.pages_fetched += 1
        html = fetched["text"]
        body = self._extract(html) or ""
        payload: dict = {
            "url": url,
            "final_url": fetched.get("final_url"),
            "status": fetched.get("status"),
        }
        if _PARKED_RE.search(body) or _PARKED_RE.search(html[:5000]):
            payload.update({"body": body, "outcome": "parked"})
        elif len(body) < self.cfg.min_body_chars:
            payload.update({"body": body, "outcome": "too_short"})
        else:
            payload.update({"body": body, "outcome": "ok"})
        if kind == "homepage" and payload["outcome"] in ("ok", "too_short"):
            # too_short included: JS-heavy homepages often yield a thin
            # trafilatura body while the raw HTML still carries nav links to
            # /news — identity is not granted but activity can still be found.
            # Parked pages are excluded (their links are ad inventory).
            payload["press_links"] = self._discover_press_links(
                html, base_url=fetched.get("final_url") or url
            )
        self._cache_put(url, payload)
        self._log(url, kind=kind, outcome=payload["outcome"],
                  status=fetched.get("status"))
        return payload

    def _discover_press_links(self, html: str, *, base_url: str) -> list[str]:
        """Same-registrable-domain links whose resolved path matches the shared
        press pattern. Document order, deduped on canonical form.
        """
        base_host = _evidence.domain_of(base_url)
        seen: set[str] = set()
        out: list[str] = []
        for href in _HREF_RE.findall(html):
            href = href.strip()
            if href.lower().startswith(("mailto:", "javascript:", "tel:", "data:")):
                continue
            resolved = urljoin(base_url, href)
            parts = urlsplit(resolved)
            if parts.scheme not in ("http", "https"):
                continue
            if not _evidence.same_site(parts.netloc.lower(), base_host):
                continue
            if not _evidence._PRESS_RE.search(parts.path or "/"):
                continue
            key = _evidence.canonical_url(resolved)
            if key in seen or key == _evidence.canonical_url(base_url):
                continue
            seen.add(key)
            out.append(resolved)
        return out

    # ---------- cache (news_body discipline) ----------

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.json"

    def _cache_get(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        from event_intel.events.enrichment import _is_fresh  # lazy: avoid cycle

        if not _is_fresh(
            payload.get("fetched_at"), now=self.now, ttl_days=self.cfg.cache_ttl_days
        ):
            return None
        return payload

    def _cache_put(self, url: str, payload: dict) -> None:
        from event_intel.timeutil import normalize_utc

        payload = {"fetched_at": normalize_utc(self.now).isoformat(), **payload}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(url).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # cache is best-effort

    # ---------- live fetch (news_body pattern: byte cap + charset decode) ----------

    def _fetch_live(self, url: str) -> dict:
        import httpx

        from event_intel.acquisition.raw_fetch import get_user_agent

        kwargs: dict = {
            "timeout": self.cfg.timeout_s,
            "follow_redirects": True,
            "headers": {"User-Agent": get_user_agent()},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            with httpx.Client(**kwargs) as client, client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return {
                        "status": resp.status_code, "text": None,
                        "error": f"HTTP {resp.status_code}",
                    }
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= self.cfg.max_bytes_per_page:
                        break
                from event_intel.textenc import decode_html

                text = decode_html(
                    bytes(buf[: self.cfg.max_bytes_per_page]),
                    header_charset=resp.charset_encoding,
                )
                return {
                    "status": resp.status_code,
                    "text": text,
                    "final_url": str(resp.url),
                }
        except Exception as exc:
            return {"status": None, "text": None, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _extract(html: str) -> str | None:
        import trafilatura

        return trafilatura.extract(html, include_comments=False, include_tables=False)

    # ---------- diagnostics (R1 schema) ----------

    def _log(
        self, url: str, *, kind: str, outcome: str, status: int | None = None,
        exc: str | None = None, attempts: int = 1,
    ) -> None:
        if self.failure_log is None:
            return
        from urllib.parse import urlparse

        self.failure_log.append({
            "ts": self.now.isoformat(),
            "lane": "homepage",
            "kind": kind,
            "domain": urlparse(url).netloc,
            "url": url,
            "status": status,
            "outcome": outcome,
            "attempts": attempts,
            "exc_classes": [exc] if exc else [],
        })
