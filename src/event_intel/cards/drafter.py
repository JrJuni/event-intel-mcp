"""Draft a capability_cards.yaml from raw product source material.

LLM provider is injected — tests pass a FakeLLM, production wires AnthropicProvider.
No heavy imports at module top.

Flow:
    1. Resolve source content (inline text, or files: .md / .txt / .pdf).
    2. If too long for one shot, head-truncate at max_chars and warn.
    3. Single-shot chat_once with a strict YAML-only system prompt.
    4. Strip fences and return raw yaml string. The caller writes it to disk;
       drafter intentionally does NOT validate (the human is expected to edit
       before `validate` / `ingest`).

Why single-shot for v0: source docs for a product are usually < 20k chars. The
chunked multi-call pipeline lives in S3 (event extraction) where 25+ chunks
matter. For drafting, one accurate pass beats merging partial drafts.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.providers.llm import LLMProvider


# Hard cap on input chars to the drafter. ~20k tokens. Truncation is logged in
# the warnings list returned with the draft so the user knows.
DEFAULT_MAX_INPUT_CHARS = 60_000


SYSTEM_PROMPT = (
    "You are a B2B product marketing analyst. Your sole task is to read the "
    "supplied product source material and emit a single YAML document that "
    "matches the `capability_cards` schema v1. Output YAML only — no prose, "
    "no markdown, no code fences.\n\n"
    "Required top-level keys: schema_version (literal int 1), product_name, "
    "one_liner, capabilities, ideal_customer. Optional: buying_triggers, "
    "bad_fit, competitors.\n\n"
    "Rules:\n"
    "- Every `capability` entry must have name, 3-20 keywords, 1-10 buyer_pains, "
    "1-10 evidence_queries (each is a Brave/Google-style search phrase that would "
    "surface a buyer in pain).\n"
    "- `buying_triggers[].weight` is a float 0.0-1.0.\n"
    "- Use the requested output language for all human-readable strings.\n"
    "- If the source is thin, prefer fewer high-quality entries over inventing.\n"
)


@dataclass
class DraftResult:
    yaml_text: str
    warnings: list[str]
    model: str
    usage: dict[str, int]


def _read_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader  # lazy: pypdf isn't free

        reader = PdfReader(str(path))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    # .md / .txt / anything else — best-effort text decode
    return path.read_text(encoding="utf-8", errors="replace")


def _gather_source(
    source_kind: str,
    source_content: str,
    source_paths: list[str] | None,
) -> str:
    """Resolve source_kind + payload into a single text blob."""
    if source_kind == "text":
        return source_content or ""

    if source_kind in {"file", "files"}:
        if not source_paths:
            return ""
        parts: list[str] = []
        for raw in source_paths:
            p = Path(raw).expanduser()
            if not p.is_file():
                raise FileNotFoundError(f"source path not found: {p}")
            parts.append(f"# {p.name}\n\n{_read_path(p)}")
        return "\n\n---\n\n".join(parts)

    raise ValueError(
        f"unsupported source_kind={source_kind!r} (expected 'text' or 'file')"
    )


def _strip_fences(text: str) -> str:
    """LLMs occasionally wrap YAML in ```yaml fences. Strip them defensively."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    # drop opening fence (```yaml or ```)
    lines = lines[1:]
    # drop closing fence if present
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def draft_cards(
    *,
    source_kind: str = "text",
    source_content: str = "",
    source_paths: list[str] | None = None,
    lang: str = "en",
    llm_provider: "LLMProvider",
    max_input_chars: int = DEFAULT_MAX_INPUT_CHARS,
    max_tokens: int = 4096,
) -> DraftResult:
    """Single-shot draft of capability_cards.yaml from source material.

    Returns the raw YAML text (not validated). The caller is expected to write
    it to disk, let the human edit, then call `validate_capability_cards`.
    """
    source_text = _gather_source(source_kind, source_content, source_paths)
    warnings: list[str] = []
    if not source_text.strip():
        raise ValueError("no source material provided (source_content empty / files empty)")

    if len(source_text) > max_input_chars:
        warnings.append(
            f"source truncated from {len(source_text)} → {max_input_chars} chars "
            "(drafter is single-shot in v0)"
        )
        source_text = source_text[:max_input_chars]

    lang_clause = (
        "Write all human-readable strings in Korean (한국어)."
        if lang == "ko"
        else "Write all human-readable strings in English."
    )
    user = (
        f"{lang_clause}\n\n"
        "PRODUCT SOURCE MATERIAL:\n"
        "---\n"
        f"{source_text}\n"
        "---\n\n"
        "Emit the YAML document now. Output YAML only."
    )

    resp = llm_provider.chat_once(
        system=SYSTEM_PROMPT,
        user=user,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    yaml_text = _strip_fences(resp.text)

    return DraftResult(
        yaml_text=yaml_text,
        warnings=warnings,
        model=resp.model,
        usage=resp.usage,
    )
