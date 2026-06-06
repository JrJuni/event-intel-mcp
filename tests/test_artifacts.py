"""Phase 18T T3 — storage.artifacts: manifest round-trip + sha256 + atomic write."""
from __future__ import annotations

import hashlib

from event_intel.storage.artifacts import (
    artifact_dir,
    make_manifest,
    read_manifest,
    sha256_of,
    verify_artifact_sha256,
    write_artifact,
    write_manifest,
)

# ---- helpers ----

def _sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---- 1. write_artifact is atomic ----

def test_write_artifact_creates_file(tmp_path):
    target = write_artifact(tmp_path, "source.html", "<html>exhibitors</html>")
    assert target == tmp_path / "source.html"
    assert target.read_text(encoding="utf-8") == "<html>exhibitors</html>"


def test_write_artifact_overwrites_existing(tmp_path):
    write_artifact(tmp_path, "source.html", "old content")
    write_artifact(tmp_path, "source.html", "new content")
    assert (tmp_path / "source.html").read_text(encoding="utf-8") == "new content"


# ---- 2. write_manifest + read_manifest round-trip ----

def test_manifest_round_trip(tmp_path):
    body = b"fake artifact bytes"
    artifact_path = tmp_path / "source.html"
    artifact_path.write_bytes(body)

    manifest_dict = make_manifest(
        verdict="static_html",
        source_kind="html_file",
        source_ref=str(artifact_path),
        url="https://example.com/exhibitors",
        content_type="text/html",
        status=200,
        http_pages=1,
        artifact_path=artifact_path,
    )
    write_manifest(tmp_path, manifest_dict)

    loaded = read_manifest(tmp_path)
    assert loaded is not None
    assert loaded.verdict == "static_html"
    assert loaded.source_kind == "html_file"
    assert loaded.source_ref == str(artifact_path)
    assert loaded.url == "https://example.com/exhibitors"
    assert loaded.status == 200
    assert loaded.http_pages == 1


# ---- 3. sha256 verify ----

def test_verify_artifact_sha256_correct(tmp_path):
    content = "exhibitor list content"
    path = write_artifact(tmp_path, "source.html", content)
    expected = sha256_of(path)
    assert verify_artifact_sha256(path, expected) is True


def test_verify_artifact_sha256_mismatch(tmp_path):
    content = "original content"
    path = write_artifact(tmp_path, "source.html", content)
    assert verify_artifact_sha256(path, "0" * 64) is False


def test_verify_artifact_sha256_missing_file(tmp_path):
    assert verify_artifact_sha256(tmp_path / "nonexistent.html", "abc") is False


# ---- 4. read_manifest returns None for missing / corrupt ----

def test_read_manifest_missing_returns_none(tmp_path):
    assert read_manifest(tmp_path) is None


def test_read_manifest_corrupt_json_returns_none(tmp_path):
    (tmp_path / "manifest.json").write_text("not valid json", encoding="utf-8")
    assert read_manifest(tmp_path) is None


# ---- 5. artifact_dir uses EVENT_INTEL_ARTIFACTS_DIR env ----

def test_artifact_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_ARTIFACTS_DIR", str(tmp_path))
    d = artifact_dir(workspace_id="ws1", event_slug="evt1")
    assert d == tmp_path / "ws1" / "evt1"
    assert d.is_dir()


# ---- 6. make_manifest sha256 matches artifact file ----

def test_make_manifest_sha256_matches_file(tmp_path):
    body = "some exhibitor data"
    path = write_artifact(tmp_path, "source.html", body)
    manifest = make_manifest(
        verdict="xhr_endpoint",
        source_kind="html_file",
        source_ref=str(path),
        url="https://example.com",
        content_type="text/html",
        status=200,
        http_pages=2,
        artifact_path=path,
    )
    assert manifest["sha256"] == sha256_of(path)
    assert manifest["http_pages"] == 2
