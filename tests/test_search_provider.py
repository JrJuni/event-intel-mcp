"""BraveSearchProvider._parse must read the correct bucket per endpoint.

Regression guard for the 2026-06-05 news bug: Brave's /news/search returns
results at the TOP LEVEL (`{"type":"news","results":[...]}`), while /web/search
nests them under `web` (`{"web":{"results":[...]}}`). The parser previously read
`data["news"]["results"]` for news → always [] → news_count 0 for every company
→ evidence_floor capped at 1 → S tier structurally unreachable. The enrichment
tests used a fake provider, so the real response shape was never asserted.

Pure-function tests on sample payloads — no network, no API key.
"""
from __future__ import annotations

from event_intel.providers.search import BraveSearchProvider


# Shapes mirror the real Brave Search API responses (verified 2026-06-05).
_NEWS_PAYLOAD = {
    "type": "news",
    "query": {"original": "Snowflake"},
    "results": [
        {
            "title": "Snowflake ships new feature",
            "url": "https://example.com/a",
            "description": "A news blurb.",
            "page_age": "2026-06-03T18:30:44",
            "meta_url": {"hostname": "example.com"},
        },
        {
            "title": "Second story",
            "url": "https://example.com/b",
            "description": "Another blurb.",
        },
    ],
}

_WEB_PAYLOAD = {
    "web": {
        "results": [
            {
                "title": "Snowflake — Official Site",
                "url": "https://www.snowflake.com",
                "description": "Homepage.",
                "meta_url": {"hostname": "www.snowflake.com"},
            }
        ]
    },
    # A top-level "results" key must NOT be picked up for web kind.
    "results": [{"title": "wrong bucket", "url": "https://nope.example"}],
}


def test_news_parse_reads_top_level_results():
    out = BraveSearchProvider._parse(_NEWS_PAYLOAD, "news")
    assert len(out) == 2, "news results must come from the top-level 'results' key"
    assert out[0].title == "Snowflake ships new feature"
    assert out[0].snippet == "A news blurb."
    # page_age is parsed into published_at; missing → None (no crash).
    assert out[0].published_at is not None
    assert out[1].published_at is None


def test_web_parse_reads_nested_web_results():
    out = BraveSearchProvider._parse(_WEB_PAYLOAD, "web")
    assert len(out) == 1, "web results must come from data['web']['results'] only"
    assert out[0].url == "https://www.snowflake.com"
    assert out[0].title != "wrong bucket"


def test_news_parse_empty_when_no_results():
    assert BraveSearchProvider._parse({"type": "news"}, "news") == []


def test_published_at_tolerates_garbage():
    payload = {"type": "news", "results": [{"title": "x", "url": "y", "page_age": "not-a-date"}]}
    out = BraveSearchProvider._parse(payload, "news")
    assert out[0].published_at is None
