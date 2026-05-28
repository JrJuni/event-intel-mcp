"""Schema drift guard for capability_cards.

If `CapabilityCards.model_json_schema()` ever drifts from the committed
`schema_snapshot.json`, this test fails — forcing the author to:
  1. Bump `SCHEMA_VERSION` in `cards/schema.py`.
  2. Refresh `schema_snapshot.json` (via `event-intel export-schema` or by
     re-running the snapshot-write one-liner shown in the failure message).
  3. Update any downstream consumers (drafter prompt, examples, tests).

This is the YAML-schema-handwritten-file killer (review R2-#3): humans never
maintain JSON schema, the schema is generated from Pydantic, and drift is loud.
"""
from __future__ import annotations

import json
from pathlib import Path

from event_intel.cards.schema import SCHEMA_VERSION, CapabilityCards


_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "event_intel"
    / "cards"
    / "schema_snapshot.json"
)


def _current_schema_canonical() -> str:
    """Same canonicalization used when writing the snapshot."""
    return json.dumps(
        CapabilityCards.model_json_schema(),
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


def test_schema_snapshot_matches_current_model():
    snapshot = _SNAPSHOT_PATH.read_text(encoding="utf-8").strip()
    current = _current_schema_canonical().strip()
    assert snapshot == current, (
        "capability_cards JSON schema drifted from snapshot.\n"
        f"Snapshot file: {_SNAPSHOT_PATH}\n\n"
        "If the change is intentional:\n"
        "  1. Bump SCHEMA_VERSION in src/event_intel/cards/schema.py\n"
        "  2. Refresh the snapshot:\n"
        "     event-intel export-schema --out src/event_intel/cards/schema_snapshot.json\n"
        "  3. Re-run pytest."
    )


def test_schema_version_is_one():
    """Snapshot was written under SCHEMA_VERSION=1; mismatch means stale snapshot."""
    assert SCHEMA_VERSION == 1
    schema = CapabilityCards.model_json_schema()
    # schema_version is a Literal[1], so pydantic emits it as const.
    sv = schema["properties"]["schema_version"]
    assert sv.get("const") == 1, f"schema_version literal drifted: {sv}"


def test_strict_extra_forbid_on_root():
    """Root model must be strict — extra keys in user yaml should fail loud."""
    schema = CapabilityCards.model_json_schema()
    assert schema.get("additionalProperties") is False, (
        "CapabilityCards root must forbid additional properties (typos cost hours)"
    )
