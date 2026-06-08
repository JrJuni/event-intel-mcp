"""Y1 CS5 — contract-replay corpus: structure-preserving synthesis + deterministic
replay through the REAL extraction + roster-match wiring (no model, no network)."""
from __future__ import annotations

import unicodedata

from event_intel.eval import replay as RP


def _config(max_chars=8000):
    return {
        "extraction": {
            "max_chunks_per_event": 12,
            "max_chars_per_chunk": max_chars,
            "source_snippet_min_chars": 20,
            "extraction_confidence_min": 0.6,
        },
        "llm": {"extract_max_tokens": 1024},
    }


# ---------- structure preservation (Q4: body removal must not gut the test) ----------

def test_synthesize_preserves_length_lines_and_script():
    raw = "Acme Robotics Inc.\n株式会社モビウス 123\n  Globex\tCorp."
    syn = RP.synthesize_fixture(raw)
    assert len(syn) == len(raw)                       # length preserved
    assert syn.count("\n") == raw.count("\n")         # line boundaries preserved
    assert syn.count("\t") == raw.count("\t")         # whitespace preserved
    # CJK stays CJK so the tokenizer branch still triggers
    cjk_in = [c for c in raw if unicodedata.name(c, "").startswith("CJK")]
    cjk_out = [c for c in syn if unicodedata.name(c, "").startswith("CJK")]
    assert cjk_in and len(cjk_out) == len(cjk_in)
    # punctuation passed through unchanged (chunk/line boundaries identical)
    assert syn[18] == "\n" and "." in syn


def test_synthesize_preserves_duplicate_substrings():
    unit = "Booth A: Acme Co."
    syn = RP.synthesize_fixture(unit + unit)  # exact double → halves must be equal
    half = len(syn) // 2
    assert syn[:half] == syn[half:]  # repeated input → repeated placeholder run


def test_synthesize_replaces_body_letters():
    syn = RP.synthesize_fixture("Acme robotics 12")
    assert "Acme" not in syn and "robotics" not in syn  # body actually removed
    assert syn == "Xxxx xxxxxxxx 00"


# ---------- raw capture is gitignored ----------

def test_capture_raw_writes_under_raw_root(tmp_path):
    p = RP.capture_raw("p1/source.html", "<html>secret PII</html>", raw_root=tmp_path)
    assert p.is_file() and p.read_text(encoding="utf-8") == "<html>secret PII</html>"
    assert tmp_path in p.parents


# ---------- deterministic replay through real extraction wiring ----------

def _corpus(responses, fixture="x" * 50, roster=None):
    return RP.ReplayCorpus(
        pair="t", fixture_text=fixture, responses=responses, roster=roster or [],
    )


def test_replay_exercises_multi_chunk_splitting():
    # low cap forces multiple chunks; one recorded response per chunk.
    fixture = ("line of synthetic text here\n" * 40)  # > a few small chunks
    cfg = _config(max_chars=120)
    # count chunks the wiring will produce, then record that many responses
    from event_intel.events.extraction import _split_chunks

    n = len(_split_chunks(fixture, max_chars=120))
    assert n >= 2, "fixture should split into multiple chunks"
    responses = ['[{"name": "Acme Co", "source_snippet": "twenty chars minimum here", "extraction_confidence": 0.9}]']
    responses += ["[]"] * (n - 1)
    corpus = _corpus(responses, fixture=fixture)
    result = RP.replay_extraction(corpus, lang="en", config=cfg)
    assert result.chunks_processed == n
    assert any(c.name == "Acme Co" for c in result.candidates)


def test_replay_is_deterministic():
    fixture = "synthetic\n" * 30
    cfg = _config(max_chars=100)
    from event_intel.events.extraction import _split_chunks

    n = len(_split_chunks(fixture, max_chars=100))
    responses = ['[{"name": "Globex", "source_snippet": "warehouse automation and agvs", "extraction_confidence": 0.8}]'] + ["[]"] * (n - 1)
    a = RP.replay_extraction(_corpus(responses, fixture=fixture), lang="en", config=cfg)
    b = RP.replay_extraction(_corpus(responses, fixture=fixture), lang="en", config=cfg)
    assert [c.name for c in a.candidates] == [c.name for c in b.candidates]


# ---------- committed corpus: load → replay → match ----------

def test_committed_corpus_replays_and_matches(repo_root):
    corpus = RP.load_corpus(repo_root / "benchmarks" / "replay" / "p_static_demo")
    result, match = RP.replay_extraction_and_match(corpus, lang="en", config=_config())
    names = {c.name for c in result.candidates}
    assert {"Acme Robotics", "Globex Corporation", "Mobius Sensor"} <= names
    # roster match: 3 of 4 roster entries materialized (RoboWorks not extracted)
    assert set(match.matched) == {"r1", "r2", "r3"}
    assert "r4" not in match.matched  # bad_fit not extracted → coverage exposes it
