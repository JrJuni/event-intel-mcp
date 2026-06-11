# benchmarks/ — Y1A real-data accuracy benchmark

Plan: `~/.claude/plans/y1a-benchmark-v2.md`. This directory holds the Y1A
benchmark contract artifacts. It is **separate from the synthetic 9-cell eval**
(`tests/fixtures/eval/`, which is scoring-only regression).

## Layout (as implemented — CS1–L6)

Per-pair **directories** of JSON (the YAML row schema below is the conceptual
contract; the on-disk artifacts are `roster.json` + `sealed_labels.json`).

```
benchmarks/
  gold/<pair>/                — COMMITTED canonical artifacts only:
    roster.json               — roster_id / canonical_name / aliases / label (names only, no PII)
    sealed_labels.json        — frozen labels + per-label grade + provenance (silver|gold)
    measure_no_enrich.json    — measure report vs the no-enrich run
    measure_enriched.json     — measure report vs the Brave-enriched run
  runs/<pair>/<run_id>/run_result.json   — immutable run record (names/scores/dims; no raw text). COMMITTED.
  sources/                    — raw captured exhibitor HTML/CSV/JSON. GITIGNORED (may contain PII).
  _raw/                       — raw LLM/Brave responses + intermediates. GITIGNORED.
  _local/<pair>/              — GITIGNORED working artifacts: company_packet, labeling sheets/worksheets,
                                drafts (drafted/crossed/refined/view/claude_labels), legacy/reference labels.
  replay/<pair>/              — committed structure-preserving synthetic CI fixtures (CS5).
```

**Commit rule**: only `roster` / `sealed_labels` / `measure_*` (per pair) + `run_result.json` are committed.
Everything carrying raw exhibitor descriptions (sheets, worksheets, packets) or that is a regenerable
intermediate (company_packet) stays under `_local/` (gitignored, incl. inside `gold/`).

## Canonical gold & label grades

`sealed_labels.json` carries a per-label **grade**: `silver` (single-vendor draft, DEV-only) or
`gold` (independently adjudicated — cross-vendor agreement / search-refine / human). A **holdout** gate
measures gold only; DEV measures allow silver.

**GTC (`p1_mongodb_gtc`) canonical = the multi-vendor gold** (cross-vendor agreement + search-refine,
20/20 gold, provenance per label). The independent human labels are kept at
`_local/p1_mongodb_gtc/sealed_labels_human_reference.json`. The two disagreed on 3 of 20, all resolved
by **search evidence** (so the multi-vendor set is the better-grounded canonical):

| company | human | canonical | evidence |
|---|---|---|---|
| Anyscale | neutral | target | MongoDB Atlas RAG/multimodal partner (anyscale.com/blog, mongodb.com/developer) |
| Modal | neutral | target | MongoDB Partner Ecosystem (cloud.mongodb.com/ecosystem/modal) |
| DigitalOcean | neutral | bad_fit | pure IaaS / GPU droplets, no application data layer |

> **Caveat (DEV-only):** GTC is DEV **calibration**, not a holdout gate. Measuring the engine against the
> labeling system's own gold is mildly circular — acceptable here because (a) it's calibration and (b) the
> 3 corrections are backed by independent web evidence. The competitor **holdout** (MongoDB × AI Expo Tokyo)
> stays blind. `prompt_sha` is empty in cross-vendor provenance (not passed in this DEV run) — a recorded
> DEV provenance limitation, not back-filled by guessing.

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
Used by **P4 (Hyodol customer)** — **now DEV** (labels were inspected this session,
so it can't be a holdout; review R2-3). The competitor holdout is **MongoDB × AI
Expo Tokyo**, kept blind until its one-shot holdout run.

> **Product lesson:** the analyzer/probe must resolve `<base href>` and statically
> scan a SPA's referenced JS bundles for endpoint literals (`axios.get`/`fetch`/
> `_ajax/`/`.json`). Doing so turns a false `operator_capture_required` into an
> auto `xhr` verdict with a candidate endpoint — the main lever for minimizing
> user intervention. See the design note below.

## ZNC pairs (p5–p7, 2026-06-11) — selection criteria & artifacts

Three DEV gold pairs produced by the zero-config news plan (G1–G4), all
captured **keylessly** (no operator browser capture — product north star):

| pair | product (card ws) | event | capture method | companies |
|---|---|---|---|---|
| `p5_snowflake_bdldn` | Snowflake (`snowflake`) | Big Data LDN 2026 (en) | public `exhibitors-sitemap.xml` | 179 |
| `p6_navercloud_aiexpo` | NAVER Cloud HyperCLOVA X (`navercloud`) | AI EXPO KOREA 2026 (ko) | static page (EUC-KR meta-charset corrected) | 197 |
| `p7_siemens_hannover` | Siemens Digital Industries (`siemens_di`) | Hannover Messe 2026 (en) | A–Z short-index crawl (28 pages, 1s courtesy) | 2,885 |

Selection criteria (plan-fixed): famous product company (news-rich, cards
draftable from public material) / publicly reachable exhibitor list (keyless)
/ domain+language diversity vs existing pairs / **no holdout contact**
(MongoDB × AI Expo Tokyo stays blind).

Labeling: top10+decoy cohort (20/pair), L0–L6 multi-vendor (GPT-OAuth draft →
independent Claude with input-SHA proof → host web-search refine → seal). All
three sealed 20/20 gold. New committed artifact per pair:
**`revenue_tiers.json`** (`revenue-tiers/v1`, host judgment ≥$10M) — the
denominator for the ADVISORY `news_capture` block on `benchmark measure`
(ZNC success criterion ⑤; not a frozen gate this cycle). Gates manifest:
`gold/thresholds_znc.json` (frozen before any label was seen).

Caveat recorded from p7: a 2,885-company roster head-truncates at the
extraction chunk cap, biasing the cohort to the alphabet head — large rosters
need a sampling strategy before their measures are comparable.
