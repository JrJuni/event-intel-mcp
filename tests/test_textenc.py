"""textenc — meta-charset sniffing decode (ZNC follow-up; live EUC-KR case)."""
from __future__ import annotations

from event_intel.textenc import decode_html, sniff_charset

EUC_KR_PAGE = (
    "<html><head><meta charset=\"euc-kr\"></head>"
    "<body><div>참가기업 갤러리 이스트소프트</div></body></html>"
).encode("euc-kr")


# ---------- sniff_charset ----------


def test_sniffs_meta_charset_variants():
    assert sniff_charset(b'<meta charset="euc-kr">') == "euc_kr"
    assert sniff_charset(b"<meta charset='Shift_JIS'>") == "shift_jis"
    assert sniff_charset(
        b'<meta http-equiv="Content-Type" content="text/html; charset=EUC-KR">'
    ) == "euc_kr"
    assert sniff_charset(b'<?xml version="1.0" encoding="ISO-8859-1"?>') == "iso8859-1"


def test_sniff_unknown_or_absent_charset_is_none():
    assert sniff_charset(b"<html><body>no declaration</body></html>") is None
    assert sniff_charset(b'<meta charset="not-a-real-codec-9000">') is None
    assert sniff_charset(b"") is None


def test_sniff_only_reads_the_head():
    page = b"x" * 5000 + b'<meta charset="euc-kr">'
    assert sniff_charset(page) is None  # declaration past 4 KiB is ignored


# ---------- decode_html ----------


def test_euc_kr_page_without_header_charset_decodes_correctly():
    """The live failure shape (AI EXPO KOREA): bare text/html header + EUC-KR
    meta. Pre-fix this decoded every Hangul char to U+FFFD."""
    text = decode_html(EUC_KR_PAGE, header_charset=None)
    assert "참가기업" in text and "이스트소프트" in text
    assert "�" not in text


def test_header_charset_wins_over_meta():
    # Body is really UTF-8 but meta lies — an explicit header is authoritative.
    page = '<meta charset="euc-kr"><div>한글</div>'.encode()
    assert "한글" in decode_html(page, header_charset="utf-8")


def test_invalid_header_charset_falls_back_to_sniff():
    assert "참가기업" in decode_html(EUC_KR_PAGE, header_charset="bogus-charset")


def test_no_declarations_defaults_to_utf8_replace_safe():
    assert decode_html("plain utf-8 텍스트".encode()) == "plain utf-8 텍스트"
    # Garbage bytes never raise.
    out = decode_html(b"\xb1\xe2\xff\xfe<html>", header_charset=None)
    assert isinstance(out, str)


# ---------- integration: B1 body fetch keeps Korean bodies intact ----------


def test_news_body_fetch_live_decodes_euc_kr(tmp_path):
    import httpx

    from event_intel.events.news_body import NewsBodyConfig, NewsBodyFetcher

    article = (
        "<html><head><meta charset=\"euc-kr\"></head><body><article>"
        + "".join(f"<p>이스트소프트가 신제품을 발표했다 {i}</p>" for i in range(10))
        + "</article></body></html>"
    ).encode("euc-kr")

    def handler(request):
        return httpx.Response(200, content=article,
                              headers={"content-type": "text/html"})  # no charset

    f = NewsBodyFetcher(
        cfg=NewsBodyConfig.from_dict({"enabled": True, "min_body_chars": 10}),
        cache_dir=tmp_path / "b", now=__import__("datetime").datetime(
            2026, 6, 11, tzinfo=__import__("datetime").UTC),
        transport=httpx.MockTransport(handler),
    )
    result = f._fetch_live("https://news.example.co.kr/a1")
    assert "이스트소프트" in result["text"]
    assert "�" not in result["text"]
