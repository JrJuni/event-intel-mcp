"""Phase 18V — labeled eval matrix.

Two layers (kept import-cold; only stdlib + cold scoring modules at module top):

- **Scoring matrix** (`harness.run_scoring_cell`) — fast regression over labeled
  cells built from fake `FitResult`. Catches scoring-math / floor / tier-rule drift.
- **Pipeline-contract matrix** — fake Search/Embedding/VectorStore injected, real
  enrichment + retriever executed. Catches pool-split / URL-classification /
  evidence-dedupe / cache-resume bugs. Lives in `tests/test_eval_matrix.py` (it
  needs test fakes), this package only ships the metrics + scoring runner.

Metrics live in `metrics.py`. Both are pure — no torch / chromadb / network.
"""
from __future__ import annotations
