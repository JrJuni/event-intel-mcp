# benchmarks/ — Y1A real-data accuracy benchmark

Plan: `~/.claude/plans/y1a-benchmark-v2.md`. This directory holds the Y1A
benchmark contract artifacts. It is **separate from the synthetic 9-cell eval**
(`tests/fixtures/eval/`, which is scoring-only regression).

## Layout

```
benchmarks/
  gold/<pair>.yaml      — gold labels (roster_id + canonical_name + aliases + label). COMMITTED.
  sources/              — raw captured exhibitor HTML/CSV. GITIGNORED (may contain PII).
  runs/<pair>/          — run_summary.* (reproducibility metadata). committed (sources excluded).
```

## PII policy

Real exhibition listing pages often expose officer names / contact info next to
company names. **`sources/` is gitignored.** `gold/*.yaml` carry only company
names + labels (no PII). Strip any PII before it lands anywhere committed.

## Gold row schema (plan D3.1)

```yaml
pair: hyodol_customer_x_hcr        # <product>_<mode>_x_<event>
product: hyodol
mode: customer
event: hcr_2025
event_url: https://www.hcr-web.jp/exhibitor/
captured_at: "2026-06-07"
labeler: tyrical
labeled_at: ""
label_basis: "public company homepage / program; engine output NOT consulted (blind packet)"
completeness: top_n            # top_n | full_roster
# universe caps MUST be explicit positive ints — 0 is NOT "full": extraction reads
# `chunks[:0]` (empty) and enrichment falls back to default 30. Set to the real
# numbers needed to score the intended universe (A=full: chunks ≥ ceil(chars/8000),
# companies ≥ roster size; B=subset: list the subset roster_id/seed).
universe: { mode: A_full, max_chunks_per_event: 40, max_companies: 320, subset_roster_ids: [], seed: 0, budget_note: "" }
target_mode_criteria:
  target: ""
  competitor: ""
  bad_fit: ""
rows:
  - roster_id: hcr-001
    canonical_name: ""
    aliases: []
    label: target            # target | competitor | bad_fit | neutral
```

`rank` is NOT stored in gold — it lives in the run-result (blind labeling, plan D3).

## Capture procedures (per event)

### E4 — H.C.R. 国際福祉機器展 2025 (`hcr_2025`)  — **automated XHR (no operator)**

`analyze-page` first returned **operator_capture_required** (0.82) — a FALSE
positive. The page is a Vue SPA with `<base href="https://www.hcr-web.jp/">`, and
the analyzer did not resolve `<base>` when looking for endpoints. Resolving it,
the page's own bundle `assets/js/exhibitor_list.js` calls:

```
GET https://www.hcr-web.jp/_ajax/exhibitor/get_exhibitor_data/
  headers: Referer https://www.hcr-web.jp/exhibitor/ , X-Requested-With XMLHttpRequest
  → 200 application/json, ~577 KB, {"company_data": [ ...296 companies... ]}
```

Each record: `company_id`, `company_title` (JP), `company_title_en` (EN),
`company_title_kana`, `company_exhibitor_zone` (1–19), `company_entry_pr`
(self-description). JP/EN/kana → natural `canonical_name` + `aliases`; zone →
stratified subset labeling; PR text → extraction input. No PII.

Re-capture (zero user intervention, reproducible):
```bash
~/miniconda3/envs/event-intel/python.exe - <<'PY'
import urllib.request, pathlib
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
req=urllib.request.Request("https://www.hcr-web.jp/_ajax/exhibitor/get_exhibitor_data/",
  headers={"User-Agent":UA,"Referer":"https://www.hcr-web.jp/exhibitor/","X-Requested-With":"XMLHttpRequest"})
pathlib.Path("benchmarks/sources/hcr_2025.json").write_bytes(urllib.request.urlopen(req,timeout=30).read())
PY
```
Public exhibitor web is open until **2026-06-30** — keep the captured JSON.
Shared by **P4 (Hyodol customer)** + **P7 (Hyodol partner)**, both HOLDOUT — do
not inspect engine output before blind labeling.

> **Product lesson:** the analyzer/probe must resolve `<base href>` and statically
> scan a SPA's referenced JS bundles for endpoint literals (`axios.get`/`fetch`/
> `_ajax/`/`.json`). Doing so turns a false `operator_capture_required` into an
> auto `xhr` verdict with a candidate endpoint — the main lever for minimizing
> user intervention. See the design note below.
