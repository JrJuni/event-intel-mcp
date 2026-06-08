"""Y1 L5 — working labeling artifacts must be gitignored (review R1#4/R2#6)."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ignored(rel: str) -> bool:
    r = subprocess.run(
        ["git", "-C", str(_repo_root()), "check-ignore", rel],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and r.stdout.strip() != ""


def test_local_dir_and_sheets_are_gitignored():
    assert _ignored("benchmarks/_local/p1/labeling_sheet.json")
    assert _ignored("benchmarks/gold/p1/labeling_sheet.json")  # even inside gold/
    assert _ignored("benchmarks/gold/p1/worksheet.md")
    assert _ignored("benchmarks/gold/p1/ai_labels.json")
    assert _ignored("benchmarks/_raw/anything.html")


def test_committable_gold_artifacts_not_ignored():
    assert not _ignored("benchmarks/gold/p1/sealed_labels.json")
    assert not _ignored("benchmarks/gold/p1/roster.json")
    assert not _ignored("benchmarks/gold/thresholds.json")
