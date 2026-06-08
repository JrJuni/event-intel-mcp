"""W1 — sources.indexer: parsing, overlap chunking, incremental sync into
product_sources_{ws}, fingerprint/manifest, limits, and partial-failure safety.

Uses lightweight fakes (no bge-m3 / no chromadb). Chunk sizes are forced small
(max_chars=50) so chunk counts are easy to reason about.
"""
from __future__ import annotations

import pytest

from event_intel.sources import indexer as I

_TS = "2026-06-08T00:00:00+00:00"


class FakeEmbedding:
    model_id = "fake-embed"

    def __init__(self):
        self.calls = 0
        self.total_texts = 0

    def embed(self, texts):
        self.calls += 1
        self.total_texts += len(texts)
        return [[float(len(t) % 7), 0.1, 0.2] for t in texts]


class FakeVectorStore:
    def __init__(self):
        self.store: dict[str, dict] = {}  # collection -> {id: (emb, meta, doc)}
        self.meta: dict[str, dict] = {}

    def upsert(self, *, collection, ids, embeddings, metadatas, documents):
        col = self.store.setdefault(collection, {})
        for i, _id in enumerate(ids):
            col[_id] = (embeddings[i], metadatas[i], documents[i])

    def existing_ids(self, collection):
        return set(self.store.get(collection, {}).keys())

    def delete_ids(self, collection, ids):
        col = self.store.get(collection, {})
        for _id in ids:
            col.pop(_id, None)

    def set_collection_metadata(self, collection, metadata):
        self.meta.setdefault(collection, {}).update(metadata)

    def get_collection_metadata(self, collection):
        return dict(self.meta.get(collection, {}))

    # convenience for assertions
    def ids(self, collection):
        return set(self.store.get(collection, {}).keys())


def _sync(tmp_path, sources_dir, *, emb=None, vs=None, max_chars=50, overlap=10, **kw):
    emb = emb or FakeEmbedding()
    vs = vs or FakeVectorStore()
    res = I.sync_sources(
        sources_dir=sources_dir,
        workspace_id="default",
        embedding_provider=emb,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=max_chars,
        overlap=overlap,
        **kw,
    )
    return res, emb, vs


# --------------------------------------------------------------------------- #
# unit-level helpers
# --------------------------------------------------------------------------- #
def test_collection_name():
    assert I.source_collection_name("acme") == "product_sources_acme"


def test_chunk_text_single_no_overlap():
    assert I.chunk_text("short", max_chars=50, overlap=10) == ["short"]


def test_chunk_text_overlap_prefixes_previous_tail():
    text = "A" * 60 + "\n" + "B" * 60
    base = I._split_chunks(text, max_chars=50)
    assert len(base) >= 2
    out = I.chunk_text(text, max_chars=50, overlap=10)
    assert len(out) == len(base)
    assert out[0] == base[0]
    # 2nd chunk carries the last 10 chars of the 1st base chunk as a prefix
    assert out[1] == base[0][-10:] + base[1]


def test_decode_bytes_utf8_and_cp949():
    assert I._decode_bytes("안녕하세요".encode()) == "안녕하세요"
    # cp949 (Korean) is the first legacy fallback and round-trips cleanly.
    assert I._decode_bytes("안녕하세요".encode("cp949")) == "안녕하세요"


def test_decode_bytes_never_raises_on_invalid():
    # arbitrary non-decodable bytes → replaced, never an exception
    out = I._decode_bytes(b"\xff\xfe\x00\x80valid")
    assert isinstance(out, str)
    assert "valid" in out


def test_parse_csv_preserves_header_and_packs_rows(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text("name,city\nAcme,Seoul\nBeta,Tokyo\n", encoding="utf-8")
    units = I._parse_csv(p, I.SourceLimits(), max_chars=4000)
    assert len(units) == 1  # both rows packed into one unit
    suffix, text, meta = units[0]
    assert "name: Acme | city: Seoul" in text
    assert "name: Beta | city: Tokyo" in text
    assert meta == {"row_start": 1, "row_end": 2}
    assert suffix == "r1-2"


def test_parse_csv_row_cap_raises(tmp_path):
    p = tmp_path / "big.csv"
    p.write_text("a\n1\n2\n3\n", encoding="utf-8")
    with pytest.raises(I.SourceLimitError):
        I._parse_csv(p, I.SourceLimits(max_csv_rows=2), max_chars=4000)


# --------------------------------------------------------------------------- #
# end-to-end sync
# --------------------------------------------------------------------------- #
def test_sync_indexes_md_txt_csv(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("# Title\n\nsome product notes here", encoding="utf-8")
    (src / "b.txt").write_text("plain text doc", encoding="utf-8")
    (src / "c.csv").write_text("k,v\nfoo,bar\n", encoding="utf-8")

    res, emb, vs = _sync(tmp_path, src)
    assert res["ok"] is True
    assert res["partial"] is False
    assert res["collection"] == "product_sources_default"
    assert res["total_files"] == 3
    assert res["changed_files"] == 3
    assert res["chunk_count"] == len(vs.ids("product_sources_default"))
    assert res["chunk_count"] >= 3
    # fingerprint persisted on the collection + manifest written
    assert vs.get_collection_metadata("product_sources_default")["content_fingerprint"] == res[
        "content_fingerprint"
    ]
    manifest = I.read_manifest(tmp_path / "manifest.json")
    assert manifest["collection"] == "product_sources_default"
    assert set(manifest["files"]) == {"a.md", "b.txt", "c.csv"}
    assert manifest["indexed_at"] == _TS
    assert emb.calls == 1  # one batched embed for all new chunks


def test_resync_no_changes_is_noop(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("notes", encoding="utf-8")
    res1, _, vs = _sync(tmp_path, src)
    fp1 = res1["content_fingerprint"]

    # 2nd sync reuses the SAME manifest + store; nothing changed.
    emb2 = FakeEmbedding()
    res2 = I.sync_sources(
        sources_dir=src,
        workspace_id="default",
        embedding_provider=emb2,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=50,
        overlap=10,
    )
    assert res2["changed_files"] == 0
    assert res2["unchanged_files"] == 1
    assert res2["content_fingerprint"] == fp1
    assert emb2.calls == 0  # no re-embedding of unchanged files


def test_changed_file_prunes_old_chunk_orphans(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.md"
    # long → 2 base chunks → ids a.md#doc#c0, a.md#doc#c1
    f.write_text("X" * 60 + "\n" + "Y" * 60, encoding="utf-8")
    res1, _, vs = _sync(tmp_path, src)
    col = "product_sources_default"
    # X*60\nY*60 at max_chars=50 → 4 base chunks (c0..c3)
    n_initial = res1["chunk_count"]
    assert n_initial == 4
    assert "a.md#doc#c3" in vs.ids(col)

    # shrink → 1 chunk (c0) → c1..c3 become orphans and must be pruned
    f.write_text("tiny", encoding="utf-8")
    emb2 = FakeEmbedding()
    res2 = I.sync_sources(
        sources_dir=src,
        workspace_id="default",
        embedding_provider=emb2,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=50,
        overlap=10,
    )
    assert res2["changed_files"] == 1
    assert res2["deleted_orphans"] == n_initial - 1  # 3
    assert vs.ids(col) == {"a.md#doc#c0"}


def test_deleted_file_prunes_its_chunks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("keep me", encoding="utf-8")
    (src / "b.md").write_text("delete me later", encoding="utf-8")
    _, _, vs = _sync(tmp_path, src)
    col = "product_sources_default"
    assert any(i.startswith("b.md#") for i in vs.ids(col))

    (src / "b.md").unlink()
    emb2 = FakeEmbedding()
    res2 = I.sync_sources(
        sources_dir=src,
        workspace_id="default",
        embedding_provider=emb2,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=50,
        overlap=10,
    )
    assert res2["deleted_orphans"] >= 1
    assert not any(i.startswith("b.md#") for i in vs.ids(col))
    assert any(i.startswith("a.md#") for i in vs.ids(col))


def test_pipeline_change_forces_full_reindex(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("notes here", encoding="utf-8")
    _, _, vs = _sync(tmp_path, src, emb=FakeEmbedding())

    # different embedding model id → pipeline fingerprint changes → re-index all
    class OtherEmb(FakeEmbedding):
        model_id = "other-model"

    emb2 = OtherEmb()
    res2 = I.sync_sources(
        sources_dir=src,
        workspace_id="default",
        embedding_provider=emb2,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=50,
        overlap=10,
    )
    assert res2["changed_files"] == 1
    assert emb2.calls == 1


def test_fingerprint_is_deterministic_across_fresh_runs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("deterministic", encoding="utf-8")
    res1, _, _ = _sync(tmp_path / "run1", src)
    (tmp_path / "run1").mkdir(exist_ok=True)
    res_a, _, _ = _sync(tmp_path / "run1", src)
    res_b, _, _ = _sync(tmp_path / "run2", src)
    assert res_a["content_fingerprint"] == res_b["content_fingerprint"]


# --------------------------------------------------------------------------- #
# partial-failure safety
# --------------------------------------------------------------------------- #
def test_corrupt_pdf_is_partial_and_keeps_other_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "good.md").write_text("good content", encoding="utf-8")
    (src / "bad.pdf").write_bytes(b"this is not a real pdf")  # pypdf will raise

    res, _, vs = _sync(tmp_path, src)
    assert res["partial"] is True
    assert "bad.pdf" in res["failed_files"]
    assert res["orphan_cleanup_ok"] is False
    # the good file still indexed
    assert any(i.startswith("good.md#") for i in vs.ids("product_sources_default"))


def test_partial_sync_does_not_prune_orphans(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    long_md = src / "a.md"
    long_md.write_text("X" * 60 + "\n" + "Y" * 60, encoding="utf-8")
    _, _, vs = _sync(tmp_path, src)
    col = "product_sources_default"
    assert "a.md#doc#c1" in vs.ids(col)

    # shrink a.md (would orphan c1) AND introduce a parse failure in the same run
    long_md.write_text("tiny", encoding="utf-8")
    (src / "bad.pdf").write_bytes(b"nope")
    emb2 = FakeEmbedding()
    res2 = I.sync_sources(
        sources_dir=src,
        workspace_id="default",
        embedding_provider=emb2,
        vectorstore_provider=vs,
        manifest_path=tmp_path / "manifest.json",
        now_iso=_TS,
        max_chars=50,
        overlap=10,
    )
    assert res2["partial"] is True
    assert res2["deleted_orphans"] == 0  # pruning skipped on a partial scan
    assert "a.md#doc#c1" in vs.ids(col)  # stale orphan deliberately retained


# --------------------------------------------------------------------------- #
# limits
# --------------------------------------------------------------------------- #
def test_max_file_bytes_marks_file_failed(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.txt").write_text("x" * 100, encoding="utf-8")
    res, _, _ = _sync(tmp_path, src, limits=I.SourceLimits(max_file_bytes=5))
    assert "big.txt" in res["failed_files"]
    assert res["partial"] is True


def test_max_files_truncates_with_warning(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for n in range(3):
        (src / f"f{n}.txt").write_text("content", encoding="utf-8")
    res, _, _ = _sync(tmp_path, src, limits=I.SourceLimits(max_files=2))
    assert res["total_files"] == 2
    assert any("max_files" in w for w in res["warnings"])


def test_missing_sources_dir_is_empty_clean_sync(tmp_path):
    res, emb, vs = _sync(tmp_path, tmp_path / "does_not_exist")
    assert res["ok"] is True
    assert res["total_files"] == 0
    assert res["chunk_count"] == 0
    assert res["partial"] is False
    assert emb.calls == 0


def test_symlink_is_skipped(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.md").write_text("real", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    try:
        (src / "link.md").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform")
    res, _, vs = _sync(tmp_path, src)
    assert any("skipped symlink" in w for w in res["warnings"])
    assert not any(i.startswith("link.md#") for i in vs.ids("product_sources_default"))
    assert any(i.startswith("real.md#") for i in vs.ids("product_sources_default"))
