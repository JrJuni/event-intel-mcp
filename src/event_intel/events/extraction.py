"""Chunked LLM extraction of exhibitor candidates from a captured source.

Pipeline (per plan v0.5 §S3 + Contract #9/#10):
    1. Split capture.text into chunks of `max_chars_per_chunk` (default 8000).
    2. Hard-cap chunks at `max_chunks_per_event` (default 12) — review R2-#7.
       Excess chunks are dropped and a warning is emitted.
    3. For each chunk, ask the LLM for a JSON list of candidates with
       required fields {name, source_snippet}, optional {url, description}.
    4. Drop candidates with source_snippet < `source_snippet_min_chars`
       (default 20). This enforces the raw_extraction floor before anything
       else touches them.
    5. Apply lang-specific normalization (en/ko) — collapse whitespace, strip
       trailing localized suffixes (㈜, Co., Ltd., Inc., etc.) for the merge key
       only — the original name is preserved.
    6. Merge across chunks: dedupe by normalized name, keep first-seen snippet,
       union of urls + descriptions. Per-row `extraction_confidence` is set to
       the max confidence reported by the LLM for that name.
    7. Optionally drop low-confidence rows (`extraction_confidence_min`) into a
       `needs_review` bucket instead of the main candidate list.

Heavy imports stay lazy — this module is import-cold for the MCP server.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.events import llm_cache as _llm_cache

_log = logging.getLogger(__name__)

# One transient retry per chunk: a single flaky LLM call must not discard the
# work of every chunk already extracted (observed: 64-chunk run dying at chunk
# 39 after ~75 min). Constant is PROVISIONAL (docs/retry-playbook.md).
_CHUNK_RETRY_SLEEP_SECONDS = 5.0

# Cost lever #16-①: a structured CSV roster already carries name/url/description
# verbatim — chunked LLM extraction would only re-emit the same rows (p7 fullx
# measured: extraction $4.35 of a $5.06 run). When a name column is detected the
# rows convert directly to candidates with ZERO LLM calls; detection failure
# falls back to the normal LLM path. Header match is case-insensitive on the
# trimmed header; priority is list order (first hit wins).
_CSV_NAME_COLUMNS = (
    "name", "company", "company_name", "exhibitor",
    "회사명", "업체명", "참가사", "상호",
)
_CSV_URL_COLUMNS = ("url", "website", "homepage", "홈페이지")
_CSV_DESC_COLUMNS = ("description", "설명", "소개")

# plan v3 R6: known wrapping keys an LLM may use to envelope the candidate list.
# When the response is a dict (instead of the requested top-level list), unwrap
# the first matching key. Single-key dicts with a list value are also unwrapped
# as a fallback. Both paths emit a warning so the prompt can be tuned later.
_EXHIBITOR_LIST_KEYS = (
    "exhibitors", "results", "data", "items", "candidates", "companies", "rows",
)

# Cost lever #16-⑤: per-chunk user message, kept as a module constant so the
# LLM-cache prompt fingerprint and the actual prompt can never drift apart.
# Placeholders (chunk counter, source_ref, chunk body) are the VOLATILE parts —
# the fingerprint hashes this template unfilled, so re-chunked/re-ordered runs
# over the same content still hit. Changing this string auto-invalidates the
# cache via the content hash; bump llm_cache.LLM_CACHE_VERSION only when
# PARSING semantics change (see events/llm_cache.py docstring).
_USER_TEMPLATE = (
    "EVENT PAGE FRAGMENT (chunk {n}/{total}, source: {source_ref}):\n"
    "---\n"
    "{chunk}\n"
    "---\n\n"
    "Return the JSON array now."
)

if TYPE_CHECKING:
    from event_intel.events.source_capture import SourceCapture
    from event_intel.providers.llm import LLMProvider


@dataclass
class ExhibitorCandidate:
    name: str
    source_snippet: str
    url: str | None = None
    description: str | None = None
    extraction_confidence: float = 1.0
    # For audit: which chunk(s) of the source produced this row.
    chunk_indices: list[int] = field(default_factory=list)
    # Pre-triage evidence (E1): body text fetched from this exhibitor's detail
    # page (``url``) so triage scores what the company DOES, not its bare name.
    # None = not fetched / unreachable / thin → the company is UNKNOWN to triage.
    profile_text: str | None = None


@dataclass
class ExtractionResult:
    candidates: list[ExhibitorCandidate]
    needs_review: list[ExhibitorCandidate]
    dropped_low_snippet: int
    warnings: list[str]
    chunks_processed: int
    chunks_total: int
    usage: dict[str, int]
    # #16-⑤: how many of chunks_processed were served from the LLM disk cache
    # (zero spend). Actual LLM calls = chunks_processed - chunks_cached.
    chunks_cached: int = 0


SYSTEM_PROMPT_EN = (
    "You read a fragment of an exhibitor / sponsor list (from a trade show or "
    "conference page) and emit a JSON array of candidate companies you can see "
    "in this fragment.\n\n"
    "Output strictly a JSON array. Each item must have:\n"
    '  - "name" (string, required): the company\'s display name (usually a '
    "heading/title line). Do NOT use a bare domain or URL (e.g. "
    '"example.com") as the name when a real name is present.\n'
    '  - "source_snippet" (string, required): a verbatim substring (>= 20 chars) '
    "from the fragment that proves the company is listed there\n"
    '  - "url" (string, optional): the company link if present. Links may appear '
    'inline as "text (https://...)" — put the URL here, not in the name.\n'
    '  - "description" (string, optional): one-line description if present\n'
    '  - "extraction_confidence" (float 0..1, optional): your confidence the '
    "row is a real exhibitor and not a navigation item / sponsor placeholder\n\n"
    "Rules:\n"
    "- Do not invent names that are not in the fragment.\n"
    "- Do not output navigation labels, the overall page/section title, or "
    "session titles — but a per-company name IS a real exhibitor even when it "
    "appears as a heading line.\n"
    "- If the fragment contains zero exhibitors, return [].\n"
    "- Output JSON only — no prose, no markdown fences."
)

SYSTEM_PROMPT_KO = (
    "당신은 전시회/컨퍼런스 페이지의 일부분을 읽고, 그 안에 보이는 후보 회사를 "
    "JSON 배열로 출력합니다.\n\n"
    "엄격히 JSON 배열만 출력하세요. 각 항목은 다음을 가집니다:\n"
    '  - "name" (문자열, 필수): 회사의 표시명(보통 제목/헤딩 줄). 실제 회사명이 '
    '있으면 맨 도메인/URL(예: "example.com")을 name 으로 쓰지 마세요.\n'
    '  - "source_snippet" (문자열, 필수): 해당 회사가 거기 있다는 증거가 되는 '
    "fragment 내 원문 부분문자열 (>= 20자)\n"
    '  - "url" (문자열, 선택): 회사 링크가 있으면. 링크는 "텍스트 (https://...)" '
    "형태로 인라인 등장할 수 있음 — URL 은 name 이 아니라 여기에.\n"
    '  - "description" (문자열, 선택)\n'
    '  - "extraction_confidence" (0~1 float, 선택)\n\n'
    "규칙:\n"
    "- fragment 에 없는 회사를 만들어내지 마세요.\n"
    "- 네비게이션/전체 페이지·섹션 제목/세션명은 제외 — 단, 회사별 이름은 헤딩 "
    "줄로 나와도 실제 참가사임.\n"
    "- 후보가 없으면 빈 배열 [] 만 출력.\n"
    "- JSON 만 출력 — 산문/마크다운 금지."
)


def _split_chunks(text: str, *, max_chars: int) -> list[str]:
    """Split on double-newlines first (keeps card/table boundaries intact),
    fall back to single-newlines, then to hard slicing if a single line is
    longer than `max_chars`. Returns a list with at least 1 entry when text
    is non-empty.
    """
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    # Prefer double-newline boundaries; if there are none, treat single newlines
    # as boundary candidates so chunked output is still readable.
    sep = "\n\n" if "\n\n" in text else "\n"
    for piece in text.split(sep):
        piece_len = len(piece) + len(sep)
        if buf and buf_len + piece_len > max_chars:
            chunks.append(sep.join(buf))
            buf = []
            buf_len = 0
        # If a single piece is itself larger than the cap, slice it.
        while len(piece) > max_chars:
            chunks.append(piece[:max_chars])
            piece = piece[max_chars:]
        buf.append(piece)
        buf_len += piece_len
    if buf:
        chunks.append(sep.join(buf))
    return chunks


# Trailing legal-form / organisation suffixes that shouldn't affect dedupe.
# Order matters — longer first so "Co., Ltd." strips before "Ltd." alone.
_NAME_SUFFIXES = (
    "co., ltd.",
    "co.,ltd.",
    "co.ltd.",
    "co., ltd",
    "co.,ltd",
    "co. ltd",
    "co., inc.",
    "co.,inc.",
    "ltd.",
    "ltd",
    "inc.",
    "inc",
    "llc.",
    "llc",
    "gmbh",
    "corp.",
    "corp",
    "주식회사",
    "(주)",
    "㈜",
)


def _normalize_name(name: str, *, lang: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"\s+", " ", n)
    # Strip a leading "㈜" / "(주)" / "주식회사 " before the actual brand.
    if lang == "ko":
        for prefix in ("주식회사 ", "(주) ", "(주)", "㈜ ", "㈜"):
            if n.startswith(prefix):
                n = n[len(prefix):].strip()
                break
    # Strip a trailing suffix (loop once — only one legal form per name typically).
    for suffix in _NAME_SUFFIXES:
        if n.endswith(suffix):
            n = n[: -len(suffix)].rstrip(" ,.")
            break
    return n


def _parse_llm_chunk(raw: str, *, chunk_index: int) -> list[ExhibitorCandidate]:
    """Parse one LLM response. Tolerant to fences / leading prose; if the
    response is unparseable, returns [].
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # As a last-ditch attempt, try to locate the first '[' and last ']'.
        start = text.find("[")
        end = text.rfind("]")
        if start == -1 or end == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return []
    # plan v3 R6: tolerate dict-wrapped responses (`{"exhibitors": [...]}` etc.)
    # GPT-5.5 and other models frequently envelope arrays — silently dropping
    # those as zero candidates causes confusing empty tier lists.
    if isinstance(data, dict):
        unwrapped: list | None = None
        for key in _EXHIBITOR_LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                _log.warning(
                    "Extraction LLM wrapped response in dict key %r — auto-unwrapping. "
                    "Consider tuning the prompt to ensure a direct list output.",
                    key,
                )
                unwrapped = value
                break
        if unwrapped is None and len(data) == 1:
            # Single-key dict with a list value — unwrap as a last resort.
            (only_value,) = data.values()
            if isinstance(only_value, list):
                _log.warning(
                    "Extraction LLM wrapped response in a single-key dict; "
                    "auto-unwrapping the only value."
                )
                unwrapped = only_value
        if unwrapped is None:
            return []
        data = unwrapped

    if not isinstance(data, list):
        return []
    out: list[ExhibitorCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        snippet = (item.get("source_snippet") or "").strip()
        if not name or not snippet:
            continue
        conf_raw = item.get("extraction_confidence", 1.0)
        try:
            conf = max(0.0, min(1.0, float(conf_raw)))
        except (TypeError, ValueError):
            conf = 1.0
        url = (item.get("url") or "").strip() or None
        desc = (item.get("description") or "").strip() or None
        out.append(
            ExhibitorCandidate(
                name=name,
                source_snippet=snippet,
                url=url,
                description=desc,
                extraction_confidence=conf,
                chunk_indices=[chunk_index],
            )
        )
    return out


def _merge_candidates(
    rows: list[ExhibitorCandidate], *, lang: str
) -> list[ExhibitorCandidate]:
    by_key: dict[str, ExhibitorCandidate] = {}
    for row in rows:
        key = _normalize_name(row.name, lang=lang)
        if not key:
            continue
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = row
            continue
        # Merge — keep first-seen name + snippet (most likely the cleanest copy),
        # union urls/descriptions, max-out confidence, accumulate chunk indices.
        if not existing.url and row.url:
            existing.url = row.url
        if not existing.description and row.description:
            existing.description = row.description
        existing.extraction_confidence = max(
            existing.extraction_confidence, row.extraction_confidence
        )
        for ci in row.chunk_indices:
            if ci not in existing.chunk_indices:
                existing.chunk_indices.append(ci)
    return list(by_key.values())


def _csv_direct_candidates(
    capture: SourceCapture, *, snippet_min_chars: int
) -> tuple[list[ExhibitorCandidate], str] | None:
    """Convert structured CSV rows directly to candidates — no LLM.

    Returns ``(rows, name_column)`` or None when no name column is detected
    (caller falls back to chunked LLM extraction). The snippet is the row's own
    non-empty cells — CSV cells ARE the verbatim source, so the snippet floor
    keeps its audit meaning. Rows shorter than the floor get a synthetic
    ``CSV row {i} of {source_ref}: ...`` prefix so a legitimate short row (bare
    name) isn't silently dropped; the row index keeps it auditable.
    """
    rows = capture.csv_rows or []
    if not rows:
        return None
    norm_headers = {h.strip().lower(): h for h in rows[0].keys() if h and h.strip()}
    name_col = next((norm_headers[c] for c in _CSV_NAME_COLUMNS if c in norm_headers), None)
    if name_col is None:
        return None
    url_col = next((norm_headers[c] for c in _CSV_URL_COLUMNS if c in norm_headers), None)
    desc_col = next((norm_headers[c] for c in _CSV_DESC_COLUMNS if c in norm_headers), None)

    out: list[ExhibitorCandidate] = []
    for i, row in enumerate(rows):
        name = (row.get(name_col) or "").strip()
        if not name:
            continue
        # Dict order is DictReader header order; cells were strip()ed at capture.
        snippet = " | ".join(v for v in row.values() if v)
        if len(snippet) < snippet_min_chars:
            snippet = f"CSV row {i} of {capture.source_ref}: {snippet}"
        url = ((row.get(url_col) or "").strip() or None) if url_col else None
        desc = ((row.get(desc_col) or "").strip() or None) if desc_col else None
        out.append(
            ExhibitorCandidate(
                name=name,
                source_snippet=snippet,
                url=url,
                description=desc,
                extraction_confidence=1.0,
                chunk_indices=[i],
            )
        )
    if not out:
        return None
    return out, name_col


def extract_exhibitors(
    *,
    capture: SourceCapture,
    lang: str = "en",
    llm_provider: LLMProvider,
    config: dict,
    max_tokens: int | None = None,
    refresh: bool = False,
    llm_cache_dir: Path | None = None,
    now: datetime | None = None,
) -> ExtractionResult:
    """Run chunked LLM extraction on a captured source.

    `config` is the dict returned by `runtime.preflight.load_config()`. We pull
    `extraction.max_chars_per_chunk`, `extraction.max_chunks_per_event`,
    `extraction.source_snippet_min_chars`, `extraction.extraction_confidence_min`
    from it so the caller controls all caps via defaults.yaml.

    #16-⑤: when `extraction.llm_cache.enabled` is true (absent key = OFF),
    per-chunk responses are cached on disk and replayed on identical re-runs.
    `refresh=True` bypasses cache reads but still writes fresh entries.
    `llm_cache_dir` / `now` are injection points for tests.
    """
    try:
        extraction_cfg = config["extraction"]
        max_chars_per_chunk = int(extraction_cfg["max_chars_per_chunk"])
        max_chunks_per_event = int(extraction_cfg["max_chunks_per_event"])
        snippet_min_chars = int(extraction_cfg["source_snippet_min_chars"])
        confidence_min = float(extraction_cfg["extraction_confidence_min"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.EXTRACTION,
            message=f"missing or invalid extraction config: {exc}",
            hint={"required": ["extraction.max_chars_per_chunk", "extraction.max_chunks_per_event",
                                "extraction.source_snippet_min_chars",
                                "extraction.extraction_confidence_min"]},
        ) from exc

    if not capture.text or not capture.text.strip():
        raise MCPError(
            error_code=ErrorCode.SOURCE_CAPTURE_FAILED,
            stage=Stage.EXTRACTION,
            message="captured source text is empty",
            hint={"source_ref": capture.source_ref, "kind": capture.kind},
        )

    warnings: list[str] = list(capture.warnings)

    # Cost lever #16-①: CSV direct conversion — structured rows skip the LLM
    # entirely. Off switch: extraction.csv_short_circuit: false (absent = on).
    # getattr: replay/eval harnesses pass duck-typed captures without csv_rows.
    csv_rows = getattr(capture, "csv_rows", None)
    if bool(extraction_cfg.get("csv_short_circuit", True)) and csv_rows:
        direct = _csv_direct_candidates(capture, snippet_min_chars=snippet_min_chars)
        if direct is not None:
            rows_direct, name_col = direct
            warnings.append(
                f"extraction: CSV short-circuit on name column {name_col!r} — "
                f"{len(rows_direct)} rows converted, 0 LLM calls"
            )
            floored = [
                r for r in rows_direct if len(r.source_snippet) >= snippet_min_chars
            ]
            dropped_low_snippet = len(rows_direct) - len(floored)
            merged = _merge_candidates(floored, lang=lang)
            return ExtractionResult(
                candidates=[
                    r for r in merged if r.extraction_confidence >= confidence_min
                ],
                needs_review=[
                    r for r in merged if r.extraction_confidence < confidence_min
                ],
                dropped_low_snippet=dropped_low_snippet,
                warnings=warnings,
                chunks_processed=0,
                chunks_total=0,
                usage={"input_tokens": 0, "output_tokens": 0},
            )
        headers = list((csv_rows[0] or {}).keys())
        warnings.append(
            "extraction: csv_short_circuit found no name column among headers "
            f"{headers!r} — falling back to LLM extraction"
        )
        _log.warning(
            "CSV short-circuit: no name column among %r; using LLM path", headers
        )

    chunks = _split_chunks(capture.text, max_chars=max_chars_per_chunk)
    chunks_total = len(chunks)
    if chunks_total > max_chunks_per_event:
        warnings.append(
            f"source produced {chunks_total} chunks; head-truncating to "
            f"{max_chunks_per_event} per max_chunks_per_event cap "
            "(set extraction.max_chunks_per_event in config to override)"
        )
        chunks = chunks[:max_chunks_per_event]

    system_prompt = SYSTEM_PROMPT_KO if lang == "ko" else SYSTEM_PROMPT_EN
    chosen_max_tokens = max_tokens or int(config.get("llm", {}).get("extract_max_tokens", 4096))

    # #16-⑤: per-chunk LLM response cache. Absent config key = OFF (legacy
    # behaviour); defaults.yaml ships it enabled. max_tokens joins the prompt
    # fingerprint — a different cap can truncate the response differently.
    cache_cfg = extraction_cfg.get("llm_cache") or {}
    cache: _llm_cache.LlmExtractionCache | None = None
    prompt_sha = ""
    if isinstance(cache_cfg, dict) and bool(cache_cfg.get("enabled", False)):
        root = (
            Path(llm_cache_dir)
            if llm_cache_dir is not None
            else Path.home() / ".event-intel" / "cache" / "llm"
        )
        ttl_raw = cache_cfg.get("ttl_days", 14)
        ttl_days = None if ttl_raw is None else int(ttl_raw)
        cache = _llm_cache.LlmExtractionCache(root, ttl_days=ttl_days)
        prompt_sha = _llm_cache.prompt_fingerprint(
            system_prompt, _USER_TEMPLATE, f"max_tokens={chosen_max_tokens}"
        )
    cache_model = str(getattr(llm_provider, "model", "") or "")
    now_dt = now or datetime.now(UTC)

    all_rows: list[ExhibitorCandidate] = []
    usage_acc = {"input_tokens": 0, "output_tokens": 0}
    chunks_cached = 0
    for i, chunk in enumerate(chunks):
        if cache is not None and not refresh:
            cached_text = cache.get(
                model=cache_model, lang=lang, prompt_sha=prompt_sha,
                chunk_text=chunk, now=now_dt,
            )
            if cached_text is not None:
                # Re-parse with the CURRENT index — never a stored one — so
                # chunk attribution stays correct across re-chunking.
                chunks_cached += 1
                all_rows.extend(_parse_llm_chunk(cached_text, chunk_index=i))
                continue
        user = _USER_TEMPLATE.format(
            n=i + 1, total=len(chunks), source_ref=capture.source_ref, chunk=chunk
        )
        resp = None
        for attempt in (0, 1):
            try:
                resp = llm_provider.chat_once(
                    system=system_prompt,
                    user=user,
                    max_tokens=chosen_max_tokens,
                    temperature=0.0,
                )
                break
            except Exception as exc:
                if attempt == 0:
                    _log.warning("extraction chunk %d failed once (%s); retrying", i, exc)
                    warnings.append(
                        f"extraction: chunk {i} LLM call failed once; retried ({exc})"
                    )
                    time.sleep(_CHUNK_RETRY_SLEEP_SECONDS)
                    continue
                raise MCPError(
                    error_code=ErrorCode.UPSTREAM_ERROR,
                    stage=Stage.EXTRACTION,
                    message=f"LLM extraction call failed on chunk {i} (after 1 retry): {exc}",
                    hint={"chunk_index": i, "chunks_total": len(chunks)},
                    retryable=True,
                ) from exc
        usage_acc["input_tokens"] += int(resp.usage.get("input_tokens", 0) or 0)
        usage_acc["output_tokens"] += int(resp.usage.get("output_tokens", 0) or 0)
        if cache is not None:
            cache.put(
                model=cache_model, lang=lang, prompt_sha=prompt_sha,
                chunk_text=chunk, response_text=resp.text, now=now_dt,
            )
        all_rows.extend(_parse_llm_chunk(resp.text, chunk_index=i))

    if chunks_cached:
        warnings.append(
            f"extraction: {chunks_cached}/{len(chunks)} chunks served from LLM "
            "cache (0 tokens spent on those)"
        )

    # raw_extraction floor — snippet < min chars are dropped silently (not
    # routed to needs_review per Contract #9). Track count for audit.
    floored = [r for r in all_rows if len(r.source_snippet) >= snippet_min_chars]
    dropped_low_snippet = len(all_rows) - len(floored)

    merged = _merge_candidates(floored, lang=lang)

    needs_review = [r for r in merged if r.extraction_confidence < confidence_min]
    accepted = [r for r in merged if r.extraction_confidence >= confidence_min]

    return ExtractionResult(
        candidates=accepted,
        needs_review=needs_review,
        dropped_low_snippet=dropped_low_snippet,
        warnings=warnings,
        chunks_processed=len(chunks),
        chunks_total=chunks_total,
        usage=usage_acc,
        chunks_cached=chunks_cached,
    )
