"""Phase 18W P2-4 — cards-backed CJK category_fit measurement (REPORT-ONLY).

Why this exists: `score_category_fit` is the ONLY consumer of the rule-based CJK
bigram tokenizer, and it returns 0.0 whenever `cards is None`. The eval matrix
harness runs with `cards=None`, so adding JP/CN cells there would never exercise
the tokenizer (review r2 #1/#4). This suite builds REAL `CapabilityCards` with
CJK `ideal_customer.industries` and calls `score_category_fit` directly, so the
bigram tokenizer is genuinely measured.

It is deliberately NOT a quality gate: it asserts only the structural invariant
that a verbatim industry phrase in the description scores > 0 (proving the
cards-backed path works), and it MEASURES — without failing on — the false
overlap rate where an unrelated CJK word shares a 2-char window with a needle.
A janome/jieba morphological backend (Step 1) is justified only if these
measurements show the bigram approach is materially insufficient.
"""
from __future__ import annotations

import pytest

from event_intel.cards.schema import Capability, CapabilityCards, IdealCustomer
from event_intel.errors import ErrorCode, MCPError
from event_intel.events.enrichment import EnrichedExhibitor
from event_intel.scoring import cjk as _cjk
from event_intel.scoring.cjk import CjkSpec, make_cjk_spec, resolve_segmenter
from event_intel.scoring.dimensions import score_category_fit


def _cards(industries: list[str]) -> CapabilityCards:
    return CapabilityCards(
        product_name="P",
        one_liner="one liner",
        capabilities=[
            Capability(
                name="cap", keywords=["alpha", "beta", "gamma"],
                buyer_pains=["pain"], evidence_queries=["q"],
            )
        ],
        ideal_customer=IdealCustomer(
            industries=industries, company_signals=["enterprise"], geo=[],
        ),
    )


def _cat_fit(industries: list[str], description: str) -> float:
    row = EnrichedExhibitor(name="X", source_snippet="s", description=description)
    return score_category_fit(row, cards=_cards(industries))


# (lang, industry needle, description that genuinely IS about that industry)
_POSITIVE = [
    ("ja", "半導体製造装置", "弊社は半導体製造装置の検査ソリューションを提供します"),
    ("ja", "産業用ロボット", "産業用ロボットの制御システムを開発しています"),
    ("ja", "自動車部品", "自動車部品の精密加工メーカーです"),
    ("zh", "新能源汽车", "我们生产新能源汽车的电池管理系统"),
    ("zh", "工业自动化", "提供工业自动化与智能制造解决方案"),
    ("ko", "반도체장비", "반도체장비 검사 솔루션을 제공합니다"),
    ("ko", "이차전지", "이차전지 소재와 셀을 개발합니다"),
]

# (lang, industry needle, UNRELATED description that shares a 2-char window)
_NEGATIVE = [
    ("ja", "半導体", "弊社の指導体制を刷新しました"),        # 導体 ⊂ 指導体制 (guidance system)
    ("zh", "半导体", "公司优化了领导体系和管理流程"),         # 导体 ⊂ 领导体系 (leadership system)
    ("zh", "新能源汽车", "我们提供能源管理咨询服务"),          # 能源 shared, not EV
    ("ko", "이차전지", "전지적 작가 시점의 소설을 출판합니다"),  # 전지 ⊂ 전지적 (omniscient)
]


def test_cards_backed_cjk_category_fit_is_measurable():
    """Structural: a verbatim CJK industry phrase in the description scores > 0.
    This is what makes the tokenizer measurable at all (cards=None always 0)."""
    for lang, industry, desc in _POSITIVE:
        score = _cat_fit([industry], desc)
        assert score > 0.0, f"{lang} false-zero: needle {industry!r} not found in {desc!r}"


def test_cjk_bigram_false_overlap_report(capsys):
    """REPORT-ONLY: measure how often the bigram tokenizer credits category_fit
    for an UNRELATED CJK word that merely shares a 2-char window with a needle.
    No quality threshold is asserted — the numbers inform whether a morphological
    backend (janome/jieba) is worth its cold-start/packaging cost."""
    false_positives = []
    for lang, industry, desc in _NEGATIVE:
        score = _cat_fit([industry], desc)
        if score > 0.0:
            false_positives.append((lang, industry, desc, round(score, 3)))

    fp_rate = len(false_positives) / len(_NEGATIVE)
    # ascii() escapes CJK so the report is safe on a cp949 (Windows) console too —
    # the project explicitly guards Windows/Linux parity.
    print("\n[P2-4 CJK bigram measurement]")
    print(f"  positives measured: {len(_POSITIVE)} (all expected > 0)")
    print(f"  negative cases: {len(_NEGATIVE)}, false-overlap: {len(false_positives)} "
          f"(rate {fp_rate:.2f})")
    for lang, industry, desc, score in false_positives:
        print(f"    FP[{lang}] needle={ascii(industry)} desc={ascii(desc)} -> {score}")

    # Report-only: assert the harness produced a measurement, NOT a quality bar.
    assert 0.0 <= fp_rate <= 1.0


# ---------- P2-4 Step 1: morphological backend acceptance (needs [cjk]) ----------


def _cat_fit_morph(industries: list[str], description: str, language: str) -> float:
    row = EnrichedExhibitor(name="X", source_snippet="s", description=description)
    spec = CjkSpec(mode="morphological", language=language, han_default="zh")
    return score_category_fit(row, cards=_cards(industries), cjk=spec)


@pytest.mark.parametrize("lang,needle,desc", [
    ("ja", "半導体", "弊社の指導体制を刷新しました"),
    ("zh", "半导体", "公司优化了领导体系和管理流程"),
    ("zh", "新能源汽车", "我们提供能源管理咨询服务"),
])
def test_morphological_removes_jp_cn_false_overlap(lang, needle, desc):
    """The morphological backend segments on word boundaries, so a needle no
    longer collides with an unrelated word sharing a 2-char window."""
    pytest.importorskip("janome" if lang == "ja" else "jieba")
    assert _cat_fit([needle], desc) > 0.0            # bigram: false positive
    assert _cat_fit_morph([needle], desc, lang) == 0.0  # morphological: gone


@pytest.mark.parametrize("lang,needle,desc", [
    ("ja", "半導体製造装置", "弊社は半導体製造装置の検査ソリューションを提供します"),
    ("zh", "新能源汽车", "我们生产新能源汽车的电池管理系统"),
])
def test_morphological_preserves_positives(lang, needle, desc):
    """No false-zero: a verbatim industry phrase still matches under morphological."""
    pytest.importorskip("janome" if lang == "ja" else "jieba")
    assert _cat_fit_morph([needle], desc, lang) > 0.0


def test_tokenizer_language_independent_of_output_lang():
    """v3 P1 regression: the tokenizer language comes from config, NOT the output
    `lang`. make_cjk_spec honors config language; a JP spec segments JP text with
    no output-lang param anywhere in the call."""
    spec = make_cjk_spec({"mode": "morphological", "language": "ja"})
    assert spec is not None and spec.language == "ja"
    pytest.importorskip("janome")
    row = EnrichedExhibitor(name="X", source_snippet="s",
                            description="弊社は半導体製造装置を提供します")
    assert score_category_fit(row, cards=_cards(["半導体製造装置"]), cjk=spec) > 0.0


def test_segmenter_is_deterministic():
    """Symmetry precondition: the resolved segmenter is a stable function of input."""
    pytest.importorskip("janome")
    seg = resolve_segmenter(CjkSpec(mode="morphological", language="ja"), sample="半導体")
    assert seg("半導体製造装置") == seg("半導体製造装置")


def test_fallback_to_bigram_warns_on_importerror(monkeypatch):
    """morphological + missing library → bigram fallback + ONE warning (not error).
    Force the ImportError via monkeypatch rather than assuming the lib is absent —
    the [cjk] CI job HAS it installed."""
    def _boom():
        raise ImportError("simulated missing janome")

    monkeypatch.setattr(_cjk, "_janome_segment", _boom)
    monkeypatch.setattr(_cjk, "_warned", set())
    spec = CjkSpec(mode="morphological", language="ja")
    with pytest.warns(RuntimeWarning, match="falling back to bigram"):
        seg = resolve_segmenter(spec, sample="半導体")
    assert seg("半導体") == {"半導", "導体"}  # bigram behavior


def test_invalid_cjk_config_raises_config_error():
    for bad in (
        {"mode": "nope"},
        {"mode": "morphological", "language": "fr"},
        {"mode": "morphological", "han_default": "ko"},
    ):
        with pytest.raises(MCPError) as ei:
            make_cjk_spec(bad)
        assert ei.value.error_code == ErrorCode.CONFIG_ERROR


def test_bigram_mode_and_absent_config_resolve_to_none():
    """mode=bigram or absent block → None (zero-overhead default path)."""
    assert make_cjk_spec(None) is None
    assert make_cjk_spec({}) is None
    assert make_cjk_spec({"mode": "bigram"}) is None
