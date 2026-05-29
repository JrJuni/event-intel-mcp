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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage

_log = logging.getLogger(__name__)

# plan v3 R6: known wrapping keys an LLM may use to envelope the candidate list.
# When the response is a dict (instead of the requested top-level list), unwrap
# the first matching key. Single-key dicts with a list value are also unwrapped
# as a fallback. Both paths emit a warning so the prompt can be tuned later.
_EXHIBITOR_LIST_KEYS = (
    "exhibitors", "results", "data", "items", "candidates", "companies", "rows",
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


@dataclass
class ExtractionResult:
    candidates: list[ExhibitorCandidate]
    needs_review: list[ExhibitorCandidate]
    dropped_low_snippet: int
    warnings: list[str]
    chunks_processed: int
    chunks_total: int
    usage: dict[str, int]


SYSTEM_PROMPT_EN = (
    "You read a fragment of an exhibitor / sponsor list (from a trade show or "
    "conference page) and emit a JSON array of candidate companies you can see "
    "in this fragment.\n\n"
    "Output strictly a JSON array. Each item must have:\n"
    '  - "name" (string, required): the company name as it appears\n'
    '  - "source_snippet" (string, required): a verbatim substring (>= 20 chars) '
    "from the fragment that proves the company is listed there\n"
    '  - "url" (string, optional): exhibitor link if present in the fragment\n'
    '  - "description" (string, optional): one-line description if present\n'
    '  - "extraction_confidence" (float 0..1, optional): your confidence the '
    "row is a real exhibitor and not a navigation item / sponsor placeholder\n\n"
    "Rules:\n"
    "- Do not invent names that are not in the fragment.\n"
    "- Do not output navigation labels, page headings, or session titles.\n"
    "- If the fragment contains zero exhibitors, return [].\n"
    "- Output JSON only — no prose, no markdown fences."
)

SYSTEM_PROMPT_KO = (
    "당신은 전시회/컨퍼런스 페이지의 일부분을 읽고, 그 안에 보이는 후보 회사를 "
    "JSON 배열로 출력합니다.\n\n"
    "엄격히 JSON 배열만 출력하세요. 각 항목은 다음을 가집니다:\n"
    '  - "name" (문자열, 필수): 등장한 그대로의 회사명\n'
    '  - "source_snippet" (문자열, 필수): 해당 회사가 거기 있다는 증거가 되는 '
    "fragment 내 원문 부분문자열 (>= 20자)\n"
    '  - "url" (문자열, 선택)\n'
    '  - "description" (문자열, 선택)\n'
    '  - "extraction_confidence" (0~1 float, 선택)\n\n'
    "규칙:\n"
    "- fragment 에 없는 회사를 만들어내지 마세요.\n"
    "- 네비게이션/제목/세션명은 제외.\n"
    "- 후보가 없으면 빈 배열 [] 만 출력.\n"
    "- JSON 만 출력 — 산문/마크다운 금지."
)


def _split_chunks(text: str, *, max_chars: int) -> list[str]:
    """Split on double-newlines first (keeps card/table boundaries intact),
    fall back to single-newlines, then to hard slicing if a single line is
    longer than `max_chars`. Returns a list with at least 1 entry when text
    is non-empty."""
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
    response is unparseable, returns []."""
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


def extract_exhibitors(
    *,
    capture: "SourceCapture",
    lang: str = "en",
    llm_provider: "LLMProvider",
    config: dict,
    max_tokens: int | None = None,
) -> ExtractionResult:
    """Run chunked LLM extraction on a captured source.

    `config` is the dict returned by `runtime.preflight.load_config()`. We pull
    `extraction.max_chars_per_chunk`, `extraction.max_chunks_per_event`,
    `extraction.source_snippet_min_chars`, `extraction.extraction_confidence_min`
    from it so the caller controls all caps via defaults.yaml.
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

    chunks = _split_chunks(capture.text, max_chars=max_chars_per_chunk)
    warnings: list[str] = list(capture.warnings)
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

    all_rows: list[ExhibitorCandidate] = []
    usage_acc = {"input_tokens": 0, "output_tokens": 0}
    for i, chunk in enumerate(chunks):
        user = (
            "EVENT PAGE FRAGMENT (chunk "
            f"{i + 1}/{len(chunks)}, source: {capture.source_ref}):\n"
            "---\n"
            f"{chunk}\n"
            "---\n\n"
            "Return the JSON array now."
        )
        try:
            resp = llm_provider.chat_once(
                system=system_prompt,
                user=user,
                max_tokens=chosen_max_tokens,
                temperature=0.0,
            )
        except Exception as exc:
            raise MCPError(
                error_code=ErrorCode.UPSTREAM_ERROR,
                stage=Stage.EXTRACTION,
                message=f"LLM extraction call failed on chunk {i}: {exc}",
                hint={"chunk_index": i, "chunks_total": len(chunks)},
                retryable=True,
            ) from exc
        usage_acc["input_tokens"] += int(resp.usage.get("input_tokens", 0) or 0)
        usage_acc["output_tokens"] += int(resp.usage.get("output_tokens", 0) or 0)
        all_rows.extend(_parse_llm_chunk(resp.text, chunk_index=i))

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
    )
