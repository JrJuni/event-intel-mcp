"""S2 — drafter tests with a fake LLM provider."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from event_intel.cards.drafter import DEFAULT_MAX_INPUT_CHARS, draft_cards
from event_intel.providers.llm import LLMResponse


class FakeLLM:
    """Returns a canned YAML response, records every call."""

    def __init__(self, *, response: str, model: str = "fake-claude"):
        self.response = response
        self.model = model
        self.calls: list[dict] = []

    def chat_once(self, *, system, user, max_tokens=4096, temperature=0.0):
        self.calls.append(
            {
                "system": system,
                "user": user,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return LLMResponse(
            text=self.response,
            usage={"input_tokens": 100, "output_tokens": 200},
            model=self.model,
        )

    # Drafter only calls chat_once; chat_cached/ping aren't required for these tests.
    def chat_cached(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def ping(self):  # pragma: no cover
        return {"status": "ok", "model": self.model}


_GOOD_YAML = textwrap.dedent(
    """\
    schema_version: 1
    product_name: Mobius
    one_liner: Embedded NPU compiler.
    capabilities:
      - name: Quantization
        keywords:
          - INT8
          - INT4
          - quantization
        buyer_pains:
          - model too large
        evidence_queries:
          - NPU quantization accuracy
    ideal_customer:
      industries:
        - automotive
      company_signals:
        - hiring compiler engineers
    """
)


def test_drafter_text_input_returns_parseable_yaml():
    llm = FakeLLM(response=_GOOD_YAML)
    result = draft_cards(
        source_kind="text",
        source_content="A short product description.",
        lang="en",
        llm_provider=llm,
    )
    parsed = yaml.safe_load(result.yaml_text)
    assert parsed["product_name"] == "Mobius"
    assert result.model == "fake-claude"
    assert result.warnings == []


def test_drafter_strips_markdown_fences():
    fenced = "```yaml\n" + _GOOD_YAML + "```"
    llm = FakeLLM(response=fenced)
    result = draft_cards(
        source_kind="text",
        source_content="x",
        llm_provider=llm,
    )
    # Should NOT start with ``` after stripping
    assert not result.yaml_text.startswith("```")
    assert yaml.safe_load(result.yaml_text)["product_name"] == "Mobius"


def test_drafter_reads_file_sources(tmp_path):
    src = tmp_path / "product.md"
    src.write_text("# Test product\n\nWhat it does.\n", encoding="utf-8")
    llm = FakeLLM(response=_GOOD_YAML)
    result = draft_cards(
        source_kind="file",
        source_paths=[str(src)],
        llm_provider=llm,
    )
    assert "Test product" in llm.calls[0]["user"]
    assert yaml.safe_load(result.yaml_text)["product_name"] == "Mobius"


def test_drafter_truncates_oversize_input_and_warns():
    huge = "x" * (DEFAULT_MAX_INPUT_CHARS + 1000)
    llm = FakeLLM(response=_GOOD_YAML)
    result = draft_cards(
        source_kind="text",
        source_content=huge,
        llm_provider=llm,
    )
    assert any("truncated" in w for w in result.warnings)
    # source in the user prompt should be at most max_input_chars
    sent_user = llm.calls[0]["user"]
    assert "x" * DEFAULT_MAX_INPUT_CHARS in sent_user
    assert "x" * (DEFAULT_MAX_INPUT_CHARS + 1) not in sent_user


def test_drafter_empty_source_raises():
    llm = FakeLLM(response=_GOOD_YAML)
    with pytest.raises(ValueError):
        draft_cards(source_kind="text", source_content="   ", llm_provider=llm)


def test_drafter_lang_ko_includes_korean_clause():
    llm = FakeLLM(response=_GOOD_YAML)
    draft_cards(
        source_kind="text",
        source_content="x",
        lang="ko",
        llm_provider=llm,
    )
    assert "한국어" in llm.calls[0]["user"]


def test_drafter_real_fixture_md_round_trips_to_valid_cards(repo_root: Path):
    """End-to-end with the committed fixture: drafter → validator passes.

    Uses _GOOD_YAML as the LLM response — what we really verify is that the
    drafter wires text input through to a YAML output that the validator accepts.
    """
    fixture = repo_root / "tests" / "fixtures" / "cards" / "sample_source.md"
    llm = FakeLLM(response=_GOOD_YAML)
    result = draft_cards(
        source_kind="file",
        source_paths=[str(fixture)],
        llm_provider=llm,
    )
    from event_intel.cards.validator import validate_dict

    cards = validate_dict(yaml.safe_load(result.yaml_text))
    assert cards.product_name == "Mobius"
