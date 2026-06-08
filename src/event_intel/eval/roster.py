"""Roster normalization + roster↔run matching — Y1 benchmark CS2.

Builds the coverage denominator (the official roster, by `roster_id`) and matches
extracted/scored company names to it. Joining on `roster_id` — never on raw name
strings — keeps JP legal prefixes, EN romanizations, and shared booths from
mis-counting (design D3.1).

Match cardinality taxonomy (design v4 §CS2, skeptic SK-1 + review R3-4):
  1:1        exact (canonical/alias) or fuzzy above threshold → materialized.
  N:1 merge  several extracted rows → one roster entry → dedupe, count once.
  1:N split  one extracted record (parent/distributor booth) names several roster
             companies → only entities the engine MATERIALIZED as scoring rows
             count toward extraction_coverage; the rest are mention-only.
  ambiguous  one extracted name fuzzy-matches several roster entries with no clear
             winner → held for manual adjudication, excluded from auto coverage.

Pure stdlib (difflib / unicodedata / re) — import-cold.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from event_intel.eval import metrics as _metrics

# Legal suffixes / corporate-form tokens stripped before matching (lang-neutral).
# Latin forms need word boundaries; CJK/paren forms don't (and \b is unreliable
# around parens + CJK). NFKC runs first, so ㈱→"(株)" etc. are already de-circled.
_LEGAL_LATIN_RE = re.compile(
    r"\b(co\.?,?\s*ltd\.?|inc\.?|llc|corp\.?|corporation|gmbh|s\.?a\.?|"
    r"pte\.?\s*ltd\.?|limited|company)\b",
    re.IGNORECASE,
)
_LEGAL_CJK_RE = re.compile(
    r"株式会社|有限会社|合同会社|\(株\)|\(有\)|㈱|㈲|주식회사|\(주\)"
)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


@dataclass
class RosterEntry:
    roster_id: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    label: str | None = None  # target | competitor | bad_fit | neutral | None

    def norm_names(self) -> list[str]:
        return [match_norm(self.canonical_name), *(match_norm(a) for a in self.aliases)]


@dataclass
class MatchResult:
    matched: dict[str, str]            # roster_id -> extracted_name (materialized)
    method: dict[str, str]             # roster_id -> exact | levenshtein | manual
    duplicates: list[tuple[str, str]]  # (extracted, roster_id) extra N:1 rows
    ambiguous: list[str]               # extracted names needing manual adjudication
    unmatched_extracted: list[str]
    mention_only: set[str] = field(default_factory=set)  # 1:N split, not materialized


def match_norm(s: str) -> str:
    """Canonical form for matching: NFKC fold, drop legal suffixes + punctuation,
    collapse whitespace, lowercase.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    s = _LEGAL_CJK_RE.sub(" ", s)
    s = _LEGAL_LATIN_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def build_roster_from_records(
    records: list[dict[str, Any]],
    *,
    id_prefix: str,
    name_keys: tuple[str, ...],
    alias_keys: tuple[str, ...] = (),
    label_key: str | None = None,
    id_key: str | None = None,
) -> list[RosterEntry]:
    """Build RosterEntry list from raw roster records (e.g. HCR company_data).

    canonical_name = first present name_key; aliases = other name/alias keys.
    roster_id = `id_key` value if given, else `{id_prefix}-{NNN}` (stable order).
    """
    out: list[RosterEntry] = []
    for i, rec in enumerate(records, start=1):
        names = [str(rec[k]).strip() for k in name_keys if rec.get(k)]
        if not names:
            continue
        aliases = [str(rec[k]).strip() for k in (*name_keys[1:], *alias_keys) if rec.get(k)]
        rid = str(rec[id_key]) if id_key and rec.get(id_key) else f"{id_prefix}-{i:03d}"
        out.append(RosterEntry(
            roster_id=rid,
            canonical_name=names[0],
            aliases=[a for a in aliases if a and a != names[0]],
            label=(str(rec[label_key]) if label_key and rec.get(label_key) else None),
        ))
    return out


def dump_roster(roster: list[RosterEntry]) -> list[dict[str, Any]]:
    """Roster → JSON-able records (the CS8 CLI on-disk roster format)."""
    return [
        {
            "roster_id": e.roster_id,
            "canonical_name": e.canonical_name,
            "aliases": list(e.aliases),
            "label": e.label,
        }
        for e in roster
    ]


def load_roster(path: str | Path) -> list[RosterEntry]:
    """Load a roster-shaped JSON file (list of dump_roster records)."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        RosterEntry(
            roster_id=str(r["roster_id"]),
            canonical_name=str(r["canonical_name"]),
            aliases=list(r.get("aliases", [])),
            label=r.get("label"),
        )
        for r in records
    ]


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def match_roster(
    extracted: list[str],
    roster: list[RosterEntry],
    *,
    threshold: float = 0.85,
    margin: float = 0.05,
    manual_resolutions: dict[str, dict[str, Any]] | None = None,
) -> MatchResult:
    """Match extracted names to roster entries by exact → fuzzy, with cardinality.

    `manual_resolutions[extracted] = {"primary": roster_id, "mentions": [roster_id]}`
    resolves an ambiguous / 1:N case: `primary` is materialized (method=manual),
    `mentions` are mention-only (not counted in extraction_coverage, R3-4).
    """
    manual_resolutions = manual_resolutions or {}
    # exact index: norm-name -> set(roster_id)
    exact: dict[str, set[str]] = {}
    for e in roster:
        for nn in e.norm_names():
            if nn:
                exact.setdefault(nn, set()).add(e.roster_id)

    matched: dict[str, str] = {}
    method: dict[str, str] = {}
    duplicates: list[tuple[str, str]] = []
    ambiguous: list[str] = []
    unmatched: list[str] = []
    mention_only: set[str] = set()

    def _claim(rid: str, name: str, how: str) -> None:
        if rid in matched:
            duplicates.append((name, rid))  # N:1 — roster already materialized
        else:
            matched[rid] = name
            method[rid] = how

    for name in extracted:
        if name in manual_resolutions:
            res = manual_resolutions[name]
            primary = res.get("primary")
            if primary:
                _claim(primary, name, "manual")
            mention_only.update(res.get("mentions", []))
            continue

        nn = match_norm(name)
        # exact
        if nn in exact:
            rids = exact[nn]
            if len(rids) == 1:
                _claim(next(iter(rids)), name, "exact")
            else:
                ambiguous.append(name)  # one name → several roster (manual)
            continue
        # fuzzy
        scored = sorted(
            ((max(_ratio(nn, rn) for rn in e.norm_names() if rn), e.roster_id) for e in roster),
            key=lambda x: -x[0],
        )
        if not scored or scored[0][0] < threshold:
            unmatched.append(name)
            continue
        top = scored[0]
        contenders = [s for s in scored if s[0] >= threshold and top[0] - s[0] <= margin]
        if len(contenders) > 1:
            ambiguous.append(name)  # no clear winner → manual
        else:
            _claim(top[1], name, "levenshtein")

    return MatchResult(
        matched=matched, method=method, duplicates=duplicates,
        ambiguous=ambiguous, unmatched_extracted=unmatched, mention_only=mention_only,
    )


def coverage(match: MatchResult, roster: list[RosterEntry]) -> _metrics.MetricResult:
    """extraction_coverage = materialized matches / roster total (R3-4)."""
    return _metrics.extraction_coverage(
        set(match.matched), {e.roster_id for e in roster}
    )


def mention_coverage(match: MatchResult, roster: list[RosterEntry]) -> _metrics.MetricResult:
    """Weaker diagnostic: materialized ∪ mention-only / roster total (R3-4)."""
    return _metrics.mention_coverage(
        set(match.matched), match.mention_only, {e.roster_id for e in roster}
    )


def labels_by_roster_id(roster: list[RosterEntry]) -> dict[str, str]:
    """roster_id -> label (for the metric functions' gold input)."""
    return {e.roster_id: e.label for e in roster if e.label}


def tiers_by_roster_id(
    match: MatchResult, tiers_by_name: dict[str, str]
) -> dict[str, str]:
    """Project run-result tiers (keyed by extracted name) onto roster_id via the
    match, so leakage/coverage metrics share the gold's roster_id key space.
    """
    return {
        rid: tiers_by_name[name]
        for rid, name in match.matched.items()
        if name in tiers_by_name
    }
