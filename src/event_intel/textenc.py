"""HTML byte→str decoding with meta-charset sniffing (ZNC follow-up).

Servers that omit the Content-Type charset get decoded as UTF-8 by httpx's
default, mojibaking EUC-KR / Shift-JIS pages. Found live 2026-06-11: the
AI EXPO KOREA exhibitor gallery declares ``<meta charset="euc-kr">`` but sends
a bare ``text/html`` header — every Hangul character decoded to U+FFFD, which
silently breaks Korean name matching (mentions_name / entity gate) and body
text downstream.

Priority: explicit HTTP-header charset > ``<meta>``/XML-decl sniff in the
first 4 KiB > UTF-8. Always ``errors="replace"`` — decoding a body must never
raise. stdlib-only (cold-import safe; importable from providers/*).
"""
from __future__ import annotations

import codecs
import re

# Matches charset=... inside <meta charset=...>, <meta http-equiv content=
# "...; charset=..."> and <?xml ... encoding="..."?> (first 4 KiB only).
_CHARSET_RE = re.compile(
    rb"""(?:charset|encoding)\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I
)


def _lookup(name: object) -> str | None:
    """Canonical codec name, or None for unknown/garbage/non-str declarations
    (defensive: httpx attributes may be mocked or oddly typed in tests).
    """
    if not name or not isinstance(name, str):
        return None
    try:
        return codecs.lookup(name.strip()).name
    except (LookupError, ValueError):
        return None


def sniff_charset(head: bytes) -> str | None:
    """Charset declared in the document head (meta/XML decl), or None."""
    m = _CHARSET_RE.search(head[:4096])
    if not m:
        return None
    return _lookup(m.group(1).decode("ascii", errors="replace"))


def decode_html(content: bytes, *, header_charset: str | None = None) -> str:
    """Decode an HTML body: header charset > meta sniff > UTF-8, replace-safe."""
    enc = _lookup(header_charset) or sniff_charset(content) or "utf-8"
    return content.decode(enc, errors="replace")
