"""Y2.1a — storage.artifact_registry: opaque ids, sha256 metadata, workspace
isolation, size cap, TTL + pin, path-traversal guard."""
from __future__ import annotations

import pytest

from event_intel.storage import artifact_registry as R


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # All registry writes land under a tmp data root.
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_put_get_round_trip_text():
    put = R.put_artifact(workspace_id="default", content="hello world")
    assert set(put) == {"artifact_id", "content_sha256", "size"}
    got = R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"])
    assert got == b"hello world"


def test_put_get_round_trip_bytes():
    put = R.put_artifact(workspace_id="default", content=b"\x00\x01binary\xff")
    got = R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"])
    assert got == b"\x00\x01binary\xff"


def test_artifact_id_is_opaque_not_sha256():
    put = R.put_artifact(workspace_id="default", content="abc")
    # id must NOT equal (or contain) the content hash — it's a capability token
    assert put["artifact_id"] != put["content_sha256"]
    assert put["content_sha256"] not in put["artifact_id"]
    # two identical contents → DIFFERENT ids (random, not content-addressed)
    put2 = R.put_artifact(workspace_id="default", content="abc")
    assert put2["artifact_id"] != put["artifact_id"]
    assert put2["content_sha256"] == put["content_sha256"]  # sha matches (dedupe metadata)


def test_sha256_metadata_matches():
    import hashlib

    put = R.put_artifact(workspace_id="default", content="checksum me")
    assert put["content_sha256"] == hashlib.sha256(b"checksum me").hexdigest()


def test_missing_id_returns_none():
    assert R.get_artifact(workspace_id="default", artifact_id="Zm9vYmFyMTIzNDU2Nzg5") is None


def test_invalid_id_is_rejected_no_traversal():
    # path-traversal / bad shapes must not escape the workspace dir
    for bad in ("../../etc/passwd", "a/b", "", "x", "id with space", "a" * 200):
        assert R.get_artifact(workspace_id="default", artifact_id=bad) is None
        assert R.get_artifact_meta(workspace_id="default", artifact_id=bad) is None


def test_size_cap_raises():
    with pytest.raises(R.ArtifactTooLarge):
        R.put_artifact(workspace_id="default", content=b"x" * 100, max_bytes=10)


def test_workspace_isolation():
    put = R.put_artifact(workspace_id="team_a", content="secret")
    # same (valid) id requested under a different workspace → not found
    assert R.get_artifact(workspace_id="team_b", artifact_id=put["artifact_id"]) is None
    assert R.get_artifact(workspace_id="team_a", artifact_id=put["artifact_id"]) == b"secret"


def test_ttl_expiry_and_gc():
    put = R.put_artifact(workspace_id="default", content="ephemeral", ttl_seconds=100, now=1000.0)
    # before expiry
    assert R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"], now=1050.0) == b"ephemeral"
    # after expiry → not returned
    assert R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"], now=1200.0) is None
    # gc removes it
    assert R.gc(workspace_id="default", now=1200.0) == 1
    assert R.gc(workspace_id="default", now=1200.0) == 0  # already gone


def test_no_ttl_never_expires():
    put = R.put_artifact(workspace_id="default", content="permanent", now=0.0)
    assert R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"], now=1e12) == b"permanent"
    assert R.gc(workspace_id="default", now=1e12) == 0


def test_pin_survives_ttl():
    put = R.put_artifact(workspace_id="default", content="pinned", ttl_seconds=10, now=1000.0)
    assert R.set_pinned(workspace_id="default", artifact_id=put["artifact_id"], pinned=True) is True
    # past TTL but pinned → still retrievable + gc skips it
    assert R.get_artifact(workspace_id="default", artifact_id=put["artifact_id"], now=2000.0) == b"pinned"
    assert R.gc(workspace_id="default", now=2000.0) == 0
    # unpin → now collectible
    R.set_pinned(workspace_id="default", artifact_id=put["artifact_id"], pinned=False)
    assert R.gc(workspace_id="default", now=2000.0) == 1


def test_set_pinned_missing_returns_false():
    assert R.set_pinned(workspace_id="default", artifact_id="Zm9vYmFyMTIzNDU2Nzg5", pinned=True) is False
