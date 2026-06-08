"""Y1 CS1 — run_summary emitter: fingerprint determinism, unique run_id,
immutable write, full serialization."""
from __future__ import annotations

import json

import pytest

from event_intel.events import run_summary as rs


def _fp(**over):
    base = dict(
        git_sha="abc123",
        cards_fingerprint="cards-fp",
        config_fp="cfg-fp",
        source_sha256="src-sha",
        caps={"max_companies": 30, "max_chunks_per_event": 12},
        target_mode="customer",
        model_ids={"extract": "claude-sonnet-4-6", "embedding": "bge-m3"},
    )
    base.update(over)
    return rs.compute_run_fingerprint(**base)


# ---------- fingerprint: deterministic, input-sensitive ----------

def test_run_fingerprint_is_deterministic():
    assert _fp() == _fp()


def test_run_fingerprint_independent_of_dict_order():
    a = _fp(caps={"max_companies": 30, "max_chunks_per_event": 12})
    b = _fp(caps={"max_chunks_per_event": 12, "max_companies": 30})
    assert a == b  # sorted keys → order-independent


@pytest.mark.parametrize("field,val", [
    ("git_sha", "deadbeef"),
    ("cards_fingerprint", "other"),
    ("config_fp", "other"),
    ("source_sha256", "other"),
    ("target_mode", "partner"),
])
def test_run_fingerprint_changes_with_input(field, val):
    assert _fp(**{field: val}) != _fp()


def test_run_fingerprint_changes_with_caps():
    assert _fp(caps={"max_companies": 31, "max_chunks_per_event": 12}) != _fp()


# ---------- run_id: unique per attempt (immutability) ----------

def test_new_run_id_unique_even_same_slug_and_time():
    a = rs.new_run_id(slug="evt", now_iso="2026-06-08T00:00:00+00:00")
    b = rs.new_run_id(slug="evt", now_iso="2026-06-08T00:00:00+00:00")
    assert a != b, "run_id must be unique per attempt (uuid component)"
    assert a.startswith("evt-2026") and b.startswith("evt-2026")


def test_run_id_vs_fingerprint_separation():
    """Same inputs → same fingerprint but different run_id (R2-4)."""
    fp1, fp2 = _fp(), _fp()
    rid1 = rs.new_run_id(slug="e", now_iso="2026-06-08T00:00:00+00:00")
    rid2 = rs.new_run_id(slug="e", now_iso="2026-06-08T00:00:00+00:00")
    assert fp1 == fp2 and rid1 != rid2


# ---------- hashing helpers ----------

def test_sha256_text_stable_and_file_missing_none(tmp_path):
    assert rs.sha256_text("x") == rs.sha256_text("x")
    assert rs.sha256_file(tmp_path / "nope.txt") is None
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    assert rs.sha256_file(f) == rs.sha256_text("hello")


def test_git_commit_sha_returns_str():
    sha = rs.git_commit_sha()
    assert isinstance(sha, str) and sha  # 'unknown' or a real sha, never raises


def test_config_hash_deterministic_order_independent():
    assert rs.config_hash({"a": 1, "b": 2}) == rs.config_hash({"b": 2, "a": 1})


# ---------- RunSummary serialization ----------

def _summary(run_id="evt-1", fp="fp1"):
    return rs.RunSummary(
        run_id=run_id, run_fingerprint=fp, git_commit_sha="abc",
        config_fp="cfg", cards_fingerprint="cards", source_sha256="src",
        provider="anthropic", model_ids={"extract": "m", "embedding": "bge-m3"},
        reference_timestamp="2026-06-08T00:00:00+00:00", target_mode="customer",
        max_companies=30, max_chunks_per_event=12, refresh=False,
        cache_hits=2, cache_misses=3, skipped_from_resume=1, search_calls=5,
        extracted=10, enriched=8, scored=8, extraction_coverage=None,
        stages=[rs.StageStatus("scoring", True)],
        companies=[rs.CompanyScore(
            name="ACME", tier="A", final_score=6.2, evidence_floor=1,
            dimensions={"capability_fit": 0.7}, tier_reasons=["floor>=1"],
        )],
        warnings=["w1"],
    )


def test_run_summary_to_dict_json_round_trips_with_all_fields():
    d = _summary().to_dict()
    parsed = json.loads(json.dumps(d, ensure_ascii=False))
    for key in (
        "run_id", "run_fingerprint", "git_commit_sha", "config_fp",
        "cards_fingerprint", "source_sha256", "provider", "model_ids",
        "reference_timestamp", "target_mode", "max_companies",
        "max_chunks_per_event", "refresh", "cache_hits", "search_calls",
        "extracted", "enriched", "scored", "extraction_coverage",
        "stages", "companies", "warnings", "pair",
    ):
        assert key in parsed, f"missing field {key}"
    assert parsed["companies"][0]["dimensions"]["capability_fit"] == 0.7
    assert parsed["extraction_coverage"] is None  # CS2 fills later


# ---------- write: immutability guard + atomic ----------

def test_write_run_summary_creates_file(tmp_path):
    path = rs.write_run_summary(_summary(), tmp_path / "runs" / "evt-1")
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "evt-1"


def test_write_run_summary_refuses_overwrite(tmp_path):
    d = tmp_path / "run"
    rs.write_run_summary(_summary(), d)
    with pytest.raises(FileExistsError):
        rs.write_run_summary(_summary(run_id="evt-2"), d)  # immutable


def test_write_run_summary_allow_overwrite(tmp_path):
    d = tmp_path / "run"
    rs.write_run_summary(_summary(run_id="first"), d)
    path = rs.write_run_summary(_summary(run_id="second"), d, allow_overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == "second"
