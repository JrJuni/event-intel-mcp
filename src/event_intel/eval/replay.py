"""Contract-replay corpus — Y1 CS5.

The Y1 quality measurement is a one-shot auditable run against live providers
(CS4). Its raw LLM/Brave responses + intermediate artifacts hold copyright/PII,
so they are written to a gitignored path (`benchmarks/_raw/`) and NEVER committed.

What IS committed for CI is a structure-preserving SYNTHETIC fixture: every body
character is mapped to a same-script placeholder, but length, line boundaries,
Unicode script distribution, and duplicate substrings are preserved exactly. So
replaying a synthetic fixture through the REAL extraction wiring genuinely
exercises chunk splitting + snippet flooring + merge + roster matching — body
removal does not make the test meaningless (review Q4) — with no real model and
no holdout data. The replay is deterministic: a fake LLM returns recorded
per-chunk responses in call order.

Pure stdlib + cold eval/events imports (imported lazily) — import-cold,
regression-guarded by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from event_intel.eval.roster import MatchResult
    from event_intel.events.extraction import ExtractionResult

# ---------- structure-preserving synthesizer ----------


def _map_char(ch: str) -> str:
    """Map one char to a same-script placeholder, preserving structure.

    Letters collapse to a per-script representative (so the CJK/Hangul tokenizer
    branches still trigger); digits → '0'; whitespace, newlines, and punctuation
    are passed through UNCHANGED so chunk/line boundaries are identical to raw.
    The mapping is per-char deterministic, so duplicate substrings stay duplicated.
    """
    cat = unicodedata.category(ch)
    if cat == "Lu":
        return "X"
    if cat == "Ll":
        return "x"
    if cat == "Nd":
        return "0"
    if cat.startswith("L"):  # other letters: keep the script for the tokenizer
        name = unicodedata.name(ch, "")
        if name.startswith("CJK"):
            return "一"  # 一
        if name.startswith("HANGUL"):
            return "가"  # 가
        if name.startswith("HIRAGANA"):
            return "あ"  # あ
        if name.startswith("KATAKANA"):
            return "ア"  # ア
        return "x"
    return ch  # whitespace / punctuation / symbols preserved exactly


def synthesize_fixture(text: str) -> str:
    """Structure-preserving synthetic copy of `text` (see module docstring)."""
    return "".join(_map_char(c) for c in text)


# ---------- raw capture (gitignored) ----------


def default_raw_root() -> Path:
    # src/event_intel/eval/replay.py → parents[3] == <repo>
    return Path(__file__).resolve().parents[3] / "benchmarks" / "_raw"


def capture_raw(name: str, content: str, *, raw_root: str | Path | None = None) -> Path:
    """Write a raw captured artifact under the gitignored raw root. Returns its path.

    These bytes are never committed — only structure-preserving synthetic fixtures
    derived from them are.
    """
    root = Path(raw_root) if raw_root is not None else default_raw_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------- deterministic replay ----------


class FakeReplayLLM:
    """Replays recorded per-chunk extraction responses in call order. No network.

    extract_exhibitors calls chat_once once per chunk, in order, so popping from a
    queue is exactly the recorded mapping. Raises if the wiring asks for more
    chunks than were recorded (a structural mismatch the test should catch).
    """

    def __init__(self, responses: list[str], *, model: str = "replay") -> None:
        self._responses = list(responses)
        self._i = 0
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def chat_once(
        self, *, system: str, user: str, max_tokens: int, temperature: float
    ) -> SimpleNamespace:
        self.calls.append({"system": system, "user": user})
        if self._i >= len(self._responses):
            raise AssertionError(
                f"replay underflow: wiring requested chunk {self._i} but only "
                f"{len(self._responses)} responses were recorded"
            )
        text = self._responses[self._i]
        self._i += 1
        return SimpleNamespace(
            text=text,
            usage={"input_tokens": 0, "output_tokens": 0},
            model=self.model,
            stop_reason="end_turn",
        )


@dataclass
class ReplayCorpus:
    pair: str
    fixture_text: str                      # structure-preserving synthetic source
    responses: list[str]                   # recorded per-chunk LLM JSON, in order
    roster: list[dict[str, Any]] = field(default_factory=list)
    source_ref: str = "replay://synthetic"


def load_corpus(corpus_dir: str | Path) -> ReplayCorpus:
    """Load a committed replay corpus: source.synthetic.txt + responses.json
    (+ optional roster.json + meta.json).
    """
    d = Path(corpus_dir)
    meta = {}
    meta_path = d / "meta.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    roster_path = d / "roster.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8")) if roster_path.is_file() else []
    return ReplayCorpus(
        pair=meta.get("pair", d.name),
        fixture_text=(d / "source.synthetic.txt").read_text(encoding="utf-8"),
        responses=json.loads((d / "responses.json").read_text(encoding="utf-8")),
        roster=roster,
        source_ref=meta.get("source_ref", "replay://synthetic"),
    )


def replay_extraction(
    corpus: ReplayCorpus, *, lang: str, config: dict[str, Any]
) -> ExtractionResult:
    """Run the REAL extraction wiring over the synthetic fixture with the fake LLM.

    Returns the ExtractionResult. Deterministic — identical inputs → identical
    output (no randomness, fake provider replays recorded responses).
    """
    from event_intel.events import extraction as _extraction

    capture = SimpleNamespace(
        text=corpus.fixture_text,
        source_ref=corpus.source_ref,
        kind="replay",
        warnings=[],
    )
    return _extraction.extract_exhibitors(
        capture=capture,
        lang=lang,
        llm_provider=FakeReplayLLM(corpus.responses),
        config=config,
    )


def replay_extraction_and_match(
    corpus: ReplayCorpus, *, lang: str, config: dict[str, Any]
) -> tuple[ExtractionResult, MatchResult]:
    """Full contract-replay: extraction → CS2 roster match. Returns
    (ExtractionResult, MatchResult) so a test can assert the chunking AND matching
    paths were both exercised on structure-preserving input.
    """
    from event_intel.eval import roster as _roster

    result = replay_extraction(corpus, lang=lang, config=config)
    roster = _roster.load_roster_records(corpus.roster)
    match = _roster.match_roster([c.name for c in result.candidates], roster)
    return result, match
