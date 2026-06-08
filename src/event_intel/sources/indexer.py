"""Source library indexer — raw product-source documents → product_sources_{ws}.

W1 of Workspace & Source Library RAG. Walks a workspace sources directory,
parses PDF / MD / TXT / CSV into text units, chunks them (4000 chars + 400
overlap), embeds via the injected provider, and incrementally upserts into a
Chroma collection that is SEPARATE from the capability-card collection
(``product_{ws}``).

Raw source text is for DRAFTING (W3) + rationale provenance (W4) ONLY — it never
feeds a score (evidence-floor discipline). Scoring reads ``product_{ws}`` (cards);
``product_sources_{ws}`` is read only by drafting / rationale.

Incremental sync follows the CS7 atomic pattern (``cards/ingest.py``): embed +
upsert the current chunk set FIRST, then delete orphans only after a FULL clean
scan — a parse failure leaves that file's prior chunks + manifest entry intact,
skips orphan pruning, and returns ``partial=True``. Cold-import safe: pypdf /
chromadb / sentence-transformers stay lazy (pypdf imported in-body; embedding +
vectorstore providers are injected).
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from event_intel.events.extraction import _split_chunks

if TYPE_CHECKING:
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.vectorstore import VectorStoreProvider

SCHEMA_VERSION = 1
_DEFAULT_EMBEDDING_MODEL_ID = "bge-m3"
# Bump when a parser's *output* changes (different text for the same bytes) so a
# pipeline change forces a full re-index even when file shas are unchanged (CS7
# extension — processing changes invalidate the cache too).
_PARSER_VERSION = 1
_SUPPORTED_SUFFIXES = (".pdf", ".md", ".txt", ".csv")
# utf-8 first, then common Korean (cp949) / Japanese (cp932) legacy encodings.
_TEXT_ENCODINGS = ("utf-8", "cp949", "cp932")

_DEFAULT_MAX_CHARS = 4000
_DEFAULT_OVERLAP = 400


class SourceLimitError(Exception):
    """A file or the corpus exceeds a configured indexing limit."""


@dataclass(frozen=True)
class SourceLimits:
    """Hard caps. Enforced, not advisory — a 200-page scan or a runaway CSV must
    not silently embed tens of thousands of chunks.
    """

    max_files: int = 500
    max_file_bytes: int = 25 * 1024 * 1024  # 25 MiB
    max_pdf_pages: int = 300
    max_csv_rows: int = 50_000
    max_total_bytes: int = 250 * 1024 * 1024  # 250 MiB


@dataclass
class _SourceChunk:
    id: str
    text: str
    metadata: dict


def source_collection_name(workspace_id: str) -> str:
    return f"product_sources_{workspace_id}"


# --------------------------------------------------------------------------- #
# text helpers
# --------------------------------------------------------------------------- #
def _decode_bytes(raw: bytes) -> str:
    """Best-effort decode: utf-8 first, then legacy cp949 (Korean) / cp932 (JP).

    Note the legacy fallbacks are order-sensitive and ambiguous — cp949 and
    cp932 share lead-byte ranges, so cp949 can silently mis-decode genuine cp932
    bytes (and vice versa). utf-8 (the modern default) is unambiguous; the legacy
    order here is Korean-first by design. A non-utf-8 file in the *other* legacy
    encoding may mojibake rather than raise — acceptable for a best-effort source
    library (the host can re-save as utf-8 if a doc comes out garbled).
    """
    for enc in _TEXT_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def chunk_text(
    text: str, *, max_chars: int = _DEFAULT_MAX_CHARS, overlap: int = _DEFAULT_OVERLAP
) -> list[str]:
    """Boundary-respecting split (reuses extraction._split_chunks) + a deterministic
    sliding overlap: each chunk after the first is prefixed with the trailing
    ``overlap`` chars of the previous *base* chunk, so a fact split across a
    boundary is still retrievable from either side.
    """
    base = _split_chunks(text, max_chars=max_chars)
    if overlap <= 0 or len(base) <= 1:
        return base
    out = [base[0]]
    for i in range(1, len(base)):
        tail = base[i - 1][-overlap:]
        out.append(tail + base[i])
    return out


# --------------------------------------------------------------------------- #
# parsers — each returns a list of (unit_suffix, text, unit_metadata)
# --------------------------------------------------------------------------- #
def _parse_pdf(path: Path, limits: SourceLimits) -> list[tuple[str, str, dict]]:
    from pypdf import PdfReader  # lazy: pypdf isn't free

    reader = PdfReader(str(path))
    pages = reader.pages
    if len(pages) > limits.max_pdf_pages:
        raise SourceLimitError(
            f"{path.name}: {len(pages)} pages > max_pdf_pages={limits.max_pdf_pages}"
        )
    units: list[tuple[str, str, dict]] = []
    for i, page in enumerate(pages):
        text = (page.extract_text() or "").strip()
        if not text:  # scanned / image-only page — OCR is out of scope
            continue
        units.append((f"p{i + 1}", text, {"page": i + 1}))
    return units


def _parse_csv(
    path: Path, limits: SourceLimits, max_chars: int
) -> list[tuple[str, str, dict]]:
    text = _decode_bytes(path.read_bytes())
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    header = rows[0]
    data = rows[1:]
    if len(data) > limits.max_csv_rows:
        raise SourceLimitError(
            f"{path.name}: {len(data)} rows > max_csv_rows={limits.max_csv_rows}"
        )

    def _render(row: list[str]) -> str:
        return " | ".join(
            f"{header[j] if j < len(header) else f'col{j}'}: {val}"
            for j, val in enumerate(row)
        )

    # Pack consecutive rows into a unit up to max_chars so we don't emit one
    # embedding per row, while keeping row-range provenance (W4 cites CSV rows).
    units: list[tuple[str, str, dict]] = []
    buf: list[str] = []
    buf_len = 0
    start = 1
    for i, row in enumerate(data, start=1):
        line = _render(row)
        if buf and buf_len + len(line) + 1 > max_chars:
            end = start + len(buf) - 1
            units.append(
                (f"r{start}-{end}", "\n".join(buf), {"row_start": start, "row_end": end})
            )
            buf, buf_len, start = [], 0, i
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        end = start + len(buf) - 1
        units.append(
            (f"r{start}-{end}", "\n".join(buf), {"row_start": start, "row_end": end})
        )
    return units


def _parse_text(path: Path) -> list[tuple[str, str, dict]]:
    text = _decode_bytes(path.read_bytes()).strip()
    if not text:
        return []
    return [("doc", text, {})]


def _parse_file(
    path: Path, limits: SourceLimits, max_chars: int
) -> list[tuple[str, str, dict]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path, limits)
    if suffix == ".csv":
        return _parse_csv(path, limits, max_chars)
    if suffix in (".md", ".txt"):
        return _parse_text(path)
    raise SourceLimitError(f"unsupported suffix: {suffix}")  # pragma: no cover


def _units_to_chunks(
    rel: str, path: Path, units: list[tuple[str, str, dict]], max_chars: int, overlap: int
) -> list[_SourceChunk]:
    name = path.name
    suffix = path.suffix.lower()
    chunks: list[_SourceChunk] = []
    for unit_suffix, text, umeta in units:
        for k, ctext in enumerate(chunk_text(text, max_chars=max_chars, overlap=overlap)):
            chunks.append(
                _SourceChunk(
                    id=f"{rel}#{unit_suffix}#c{k}",
                    text=ctext,
                    metadata={
                        "kind": "source",
                        "source_path": rel,
                        "file_name": name,
                        "suffix": suffix,
                        "chunk_index": k,
                        **umeta,
                    },
                )
            )
    return chunks


# --------------------------------------------------------------------------- #
# discovery (symlink/junction-safe, limit-enforcing)
# --------------------------------------------------------------------------- #
def _discover(sources_dir: Path, limits: SourceLimits) -> tuple[list[Path], list[str]]:
    warnings: list[str] = []
    if not sources_dir.is_dir():
        return [], warnings
    found: list[Path] = []
    # os.walk(followlinks=False) so a symlinked/junction directory is not
    # descended into; individual symlinked files are skipped below.
    for root, dirs, files in os.walk(sources_dir, followlinks=False):
        dirs.sort()
        for fn in sorted(files):
            p = Path(root) / fn
            if p.is_symlink():
                warnings.append(f"skipped symlink: {p.relative_to(sources_dir).as_posix()}")
                continue
            if p.suffix.lower() not in _SUPPORTED_SUFFIXES:
                continue
            found.append(p)
    if len(found) > limits.max_files:
        warnings.append(
            f"found {len(found)} files > max_files={limits.max_files}; "
            f"indexing the first {limits.max_files} (sorted)"
        )
        found = found[: limits.max_files]
    return found, warnings


# --------------------------------------------------------------------------- #
# fingerprints + manifest (CS7: deterministic content_fingerprint vs instance ts)
# --------------------------------------------------------------------------- #
def _pipeline_fingerprint(model_id: str, max_chars: int, overlap: int) -> str:
    blob = f"{_PARSER_VERSION}:{model_id}:{max_chars}:{overlap}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_source_fingerprint(
    files_meta: dict[str, dict], *, pipeline_fp: str, collection: str
) -> str:
    """Deterministic, timestamp-free fingerprint of an indexed corpus.

    (file bytes sha + pipeline) fully determines the chunk set, so hashing the
    sorted (rel, sha256) pairs + pipeline + collection is sufficient — no need to
    re-hash every chunk (which would force reading unchanged files).
    """
    payload = {
        "files": sorted((rel, m.get("sha256", "")) for rel, m in files_meta.items()),
        "pipeline_fingerprint": pipeline_fp,
        "collection": collection,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def read_manifest(path: str | Path) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    """Atomically write the manifest JSON (temp + replace)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".manifest.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(manifest, ensure_ascii=False, indent=2))
        Path(tmp).replace(p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


# --------------------------------------------------------------------------- #
# incremental sync
# --------------------------------------------------------------------------- #
def sync_sources(
    *,
    sources_dir: str | Path,
    workspace_id: str,
    embedding_provider: EmbeddingProvider,
    vectorstore_provider: VectorStoreProvider,
    manifest_path: str | Path,
    now_iso: str,
    embedding_model_id: str | None = None,
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
    limits: SourceLimits | None = None,
) -> dict:
    """Incrementally index ``sources_dir`` into ``product_sources_{workspace_id}``.

    Returns a summary dict (never raises for per-file problems — those land in
    ``failed_files`` / ``warnings`` with ``partial=True``). ``now_iso`` is the
    instance timestamp written to the manifest; it is NOT part of the content
    fingerprint (CS7 receipt-vs-fingerprint split).
    """
    sources_dir = Path(sources_dir).expanduser()
    limits = limits or SourceLimits()
    model_id = embedding_model_id or getattr(
        embedding_provider, "model_id", _DEFAULT_EMBEDDING_MODEL_ID
    )
    collection = source_collection_name(workspace_id)
    pipeline_fp = _pipeline_fingerprint(model_id, max_chars, overlap)

    prev = read_manifest(manifest_path) or {}
    # A pipeline change invalidates every cached file → re-index all.
    prev_files: dict[str, dict] = (
        prev.get("files", {}) if prev.get("pipeline_fingerprint") == pipeline_fp else {}
    )

    discovered, warnings = _discover(sources_dir, limits)

    files_meta: dict[str, dict] = {}
    current_ids: list[str] = []
    to_upsert: list[_SourceChunk] = []
    changed = 0
    failed: list[str] = []
    total_bytes = 0

    def _keep_prior(rel: str, reason: str) -> None:
        # A per-file failure must never drop good data: record it and retain the
        # file's prior chunk ids (so orphan pruning won't treat them as deleted).
        warnings.append(f"{rel}: {reason}")
        failed.append(rel)
        prior = prev_files.get(rel)
        if prior:
            files_meta[rel] = prior
            current_ids.extend(prior.get("chunk_ids", []))

    for path in discovered:
        rel = path.relative_to(sources_dir).as_posix()
        try:
            size = path.stat().st_size
            if size > limits.max_file_bytes:
                raise SourceLimitError(
                    f"{size} bytes > max_file_bytes={limits.max_file_bytes}"
                )
            total_bytes += size
            if total_bytes > limits.max_total_bytes:
                raise SourceLimitError(
                    f"cumulative {total_bytes} bytes > max_total_bytes={limits.max_total_bytes}"
                )
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
        except (OSError, SourceLimitError) as exc:
            _keep_prior(rel, f"stat/read failed: {exc}")
            continue

        prior = prev_files.get(rel)
        if prior and prior.get("sha256") == sha:
            files_meta[rel] = prior  # unchanged → reuse prior chunk ids, no re-embed
            current_ids.extend(prior.get("chunk_ids", []))
            continue

        try:
            units = _parse_file(path, limits, max_chars)
        except Exception as exc:  # noqa: BLE001 — corrupt pdf / limit / decode
            _keep_prior(rel, f"parse failed: {exc}")
            continue

        chunks = _units_to_chunks(rel, path, units, max_chars, overlap)
        if not units:
            warnings.append(f"{rel}: no extractable text (skipped, e.g. scanned PDF)")
        files_meta[rel] = {
            "sha256": sha,
            "size": size,
            "units": len(units),
            "chunk_ids": [c.id for c in chunks],
        }
        current_ids.extend(c.id for c in chunks)
        to_upsert.extend(chunks)
        changed += 1

    # Embed + upsert the changed/new chunks FIRST (atomic — see module docstring).
    if to_upsert:
        texts = [c.text for c in to_upsert]
        embeddings = embedding_provider.embed(texts)
        if len(embeddings) != len(texts):
            raise RuntimeError(
                f"embedding count mismatch: {len(embeddings)} for {len(texts)} inputs"
            )
        vectorstore_provider.upsert(
            collection=collection,
            ids=[c.id for c in to_upsert],
            embeddings=embeddings,
            metadatas=[c.metadata for c in to_upsert],
            documents=texts,
        )

    # Orphan pruning ONLY after a fully clean scan. On a partial sync we keep the
    # store as-is (deleted-file cleanup waits for a clean run) so a transient
    # parse failure can never be mistaken for a deletion.
    existing_fn = getattr(vectorstore_provider, "existing_ids", None)
    existing = set(existing_fn(collection)) if callable(existing_fn) else set()
    prior_all_ids = {cid for m in prev_files.values() for cid in m.get("chunk_ids", [])}
    orphans = (existing | prior_all_ids) - set(current_ids)
    deleted = 0
    orphan_cleanup_ok = True
    if failed:
        orphan_cleanup_ok = False  # not attempted; stale orphans may remain
        if orphans:
            warnings.append(
                f"partial sync: {len(orphans)} orphan chunk(s) left in place "
                "(pruned only on a clean scan)"
            )
    elif orphans:
        deleter = getattr(vectorstore_provider, "delete_ids", None)
        if callable(deleter):
            try:
                deleter(collection, sorted(orphans))
                deleted = len(orphans)
            except Exception:  # noqa: BLE001 — new data already written; prune best-effort
                orphan_cleanup_ok = False
                warnings.append(f"orphan prune failed; {len(orphans)} stale chunk(s) remain")

    content_fp = compute_source_fingerprint(
        files_meta, pipeline_fp=pipeline_fp, collection=collection
    )

    # Persist the fingerprint on the collection (drift detection, mirrors CS7).
    setter = getattr(vectorstore_provider, "set_collection_metadata", None)
    fingerprint_persisted = False
    if callable(setter):
        try:
            setter(collection, {"content_fingerprint": content_fp, "source_index": True})
            fingerprint_persisted = True
        except Exception:  # noqa: BLE001
            fingerprint_persisted = False

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "collection": collection,
        "workspace_id": workspace_id,
        "pipeline_fingerprint": pipeline_fp,
        "content_fingerprint": content_fp,
        "embedding_model_id": model_id,
        "indexed_at": now_iso,  # instance/audit only — NOT in content_fingerprint
        "files": files_meta,
    }
    write_manifest(manifest, manifest_path)

    return {
        "ok": True,
        "collection": collection,
        "total_files": len(discovered),
        "changed_files": changed,
        "unchanged_files": len(discovered) - changed - len(failed),
        "failed_files": failed,
        "deleted_orphans": deleted,
        "chunk_count": len(current_ids),
        "warnings": warnings,
        "partial": bool(failed),
        "manifest_path": str(manifest_path),
        "content_fingerprint": content_fp,
        "pipeline_fingerprint": pipeline_fp,
        "embedding_model_id": model_id,
        "fingerprint_persisted": fingerprint_persisted,
        "orphan_cleanup_ok": orphan_cleanup_ok,
    }
