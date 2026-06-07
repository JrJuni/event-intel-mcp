"""CJK tokenizer backends for category_fit (Phase 18W P2-4 Step 1).

The default rule-based char-bigram tokenizer over-matches: two unrelated CJK
words that share a 2-char window collide (measured false-overlap 100% on
adversarial cases — tests/test_category_fit_cjk.py). An optional morphological
backend segments on word boundaries instead, so `半導体` and `指導体制` no longer
share a token:
    - Japanese → janome    (pure-Python, lazy, `[cjk]` extra)
    - Chinese  → jieba      (pure-Python, lazy, `[cjk]` extra)
    - Korean   → kiwipiepy  (NATIVE wheel + ~109 MB model, lazy, separate `[kr]`
                            extra; Phase 18X). Word-boundary segmentation removes
                            bigram-window artifact overlaps.

The segmenter is resolved ONCE per score_category_fit call and applied to BOTH
the needle and the haystack, so matching stays symmetric regardless of script.
The chosen language comes from `scoring.cjk_tokenizer.language` (independent of
the output `lang`); `auto` detects from the combined needle+haystack sample.

Heavy deps (janome/jieba) are imported LAZILY inside the @lru_cache factories so
this module is cold-start safe (regression-guarded by test_mcp_cold_start).
"""
from __future__ import annotations

import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from event_intel.errors import ErrorCode, MCPError, Stage

# Kana → definitely Japanese; Hangul → definitely Korean. Pure Han is ambiguous
# (JP kanji vs CN hanzi share codepoints) → resolved via han_default.
_KANA = re.compile(r"[぀-ゟ゠-ヿ]")
_HANGUL = re.compile(r"[가-힣]")

_VALID_MODES = ("bigram", "morphological")
_VALID_LANGUAGES = ("auto", "ja", "zh", "ko")
_VALID_HAN_DEFAULT = ("ja", "zh")


def cjk_bigrams(run: str) -> set[str]:
    """Character bigrams of a pure-CJK run ('삼성전자' → 삼성/성전/전자). The
    default backend and the fallback when a morphological library is absent.
    """
    if len(run) <= 1:
        return {run} if run else set()
    return {run[i : i + 2] for i in range(len(run) - 1)}


@dataclass(frozen=True)
class CjkSpec:
    mode: str = "bigram"
    language: str = "auto"
    han_default: str = "zh"


def make_cjk_spec(cfg: dict | None) -> CjkSpec | None:
    """Validate `scoring.cjk_tokenizer` → CjkSpec. Returns None when the block is
    absent OR mode=bigram (None = 'use the zero-overhead default bigram path').
    Invalid values raise CONFIG_ERROR (loud, not a silent wrong default).
    """
    if not cfg:
        return None
    mode = cfg.get("mode", "bigram")
    language = cfg.get("language", "auto")
    han_default = cfg.get("han_default", "zh")
    bad = []
    if mode not in _VALID_MODES:
        bad.append(("mode", mode, _VALID_MODES))
    if language not in _VALID_LANGUAGES:
        bad.append(("language", language, _VALID_LANGUAGES))
    if han_default not in _VALID_HAN_DEFAULT:
        bad.append(("han_default", han_default, _VALID_HAN_DEFAULT))
    if bad:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.SCORING,
            message="invalid scoring.cjk_tokenizer: "
            + "; ".join(f"{k}={v!r} not in {allowed}" for k, v, allowed in bad),
            hint={"field": "scoring.cjk_tokenizer"},
        )
    if mode == "bigram":
        return None
    return CjkSpec(mode=mode, language=language, han_default=han_default)


def detect_cjk_language(text: str) -> str:
    """'ja' if any kana, 'ko' if any hangul, else 'han' (ambiguous pure-Han)."""
    if _KANA.search(text):
        return "ja"
    if _HANGUL.search(text):
        return "ko"
    return "han"


@lru_cache(maxsize=1)
def _janome_segment() -> Callable[[str], set[str]]:
    from janome.tokenizer import Tokenizer

    tk = Tokenizer()

    def seg(run: str) -> set[str]:
        return {t.surface for t in tk.tokenize(run) if t.surface.strip()}

    return seg


@lru_cache(maxsize=1)
def _jieba_segment() -> Callable[[str], set[str]]:
    import jieba

    def seg(run: str) -> set[str]:
        return {w for w in jieba.cut(run) if w.strip()}

    return seg


# Korean content-morpheme tags: general/proper/bound nouns, foreign (Latin),
# number, root. Dropping josa (J*), eomi (E*), suffixes, punctuation (S*) keeps
# the comparable units and removes particle noise.
_KO_CONTENT_TAGS = frozenset({"NNG", "NNP", "NNB", "SL", "SN", "XR"})


@lru_cache(maxsize=1)
def _kiwi_segment() -> Callable[[str], set[str]]:
    # kiwipiepy is a NATIVE wheel (+ ~109 MB model) — optional `[kr]` extra,
    # imported lazily so the module stays cold-start safe.
    # Note on homonyms: score_category_fit segments each word in isolation, so a
    # compound like 전지적 parses to {지적} (not 전지) and does NOT collide with
    # 이차전지's 전지 morpheme — the documented 電池/全知 case is resolved by
    # word-boundary tokenization. A residual false-overlap would require two
    # distinct words that genuinely yield an identical content morpheme.
    from kiwipiepy import Kiwi

    kw = Kiwi()

    def seg(run: str) -> set[str]:
        return {
            t.form for t in kw.tokenize(run)
            if t.tag in _KO_CONTENT_TAGS and t.form.strip()
        }

    return seg


_warned: set[str] = set()


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        warnings.warn(msg, RuntimeWarning, stacklevel=2)


def resolve_segmenter(spec: CjkSpec | None, *, sample: str) -> Callable[[str], set[str]]:
    """Resolve ONE segmenter for a single score_category_fit call. Applied to both
    needle and haystack → symmetric matching. Falls back to bigram (one warning)
    if the morphological library is missing.
    """
    if spec is None or spec.mode == "bigram":
        return cjk_bigrams
    lang = spec.language
    if lang == "auto":
        detected = detect_cjk_language(sample)
        lang = spec.han_default if detected == "han" else detected
    try:
        if lang == "ja":
            return _janome_segment()
        if lang == "zh":
            return _jieba_segment()
        if lang == "ko":
            return _kiwi_segment()
        return cjk_bigrams  # anything else
    except ImportError as exc:
        extra = "kr" if lang == "ko" else "cjk"
        _warn_once(
            f"{spec.mode}:{lang}",
            f"CJK morphological backend for {lang!r} unavailable ({exc}); "
            f"falling back to bigram. Install with: pip install -e '.[{extra}]'",
        )
        return cjk_bigrams
