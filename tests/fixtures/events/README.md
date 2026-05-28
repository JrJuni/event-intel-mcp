# Test fixtures — events

## Tracked (synthetic)

These are deterministic fixtures used by the unit + integration test suite:

- `sample_exhibitors.html` — small English HTML with 5 exhibitors
- `sample_exhibitors.csv` — CSV variant
- `sample_exhibitors_ko.html` — Korean variant
- `large_exhibitor_page.html` — 200-entry HTML to exercise the `max_chunks_per_event=12` cap

## Gitignored (operator-collected, real exhibitions)

Anything else under this directory is gitignored — see `.gitignore`. Reason: real exhibition listing pages often expose officer names / contact info alongside company names, and committing that to a public repo gives the PII a new lifetime in search engines.

To re-collect a known one:

### Smarttech Korea 2026 (`smarttech_korea_2026.html`)

The listing page is JS-rendered. The backend AJAX endpoint paginates 50 cards per call. Grab pages 1–3 directly (150 cards) and wrap them in a minimal HTML shell:

```bash
mkdir -p /tmp/stk && cd /tmp/stk
for p in 1 2 3; do
  curl -sL -A "Mozilla/5.0" -X POST \
    "https://biz.smarttechkorea.com/biz/get_panel_com.asp" \
    -H "Referer: https://biz.smarttechkorea.com/biz/index_all.asp?MY_LANG=KOR" \
    --data "S_KEY=&PAGE=${p}&PAGE_SIZE=50&BIZ_IDX=COM_0&MY_CATE=&MY_Industry_type=&MY_Industry=&MY_Country=&MY_inout=&SH_TYPE=&FLT_HALL_TYPE=&FLT_BOOTH_HALL=&SH_CATE1=&SH_TEXT=&SH_Industry_type=&SH_Industry=&SH_Country=&SH_inout=" \
    -o "panel_p${p}.html"
done

python - <<'PY'
import pathlib
pages = [pathlib.Path(f'/tmp/stk/panel_p{p}.html').read_text(encoding='utf-8', errors='replace').split('<!--pager-->')[0] for p in (1,2,3)]
out = ('<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">'
       '<title>스마트테크코리아 2026 — 참가업체</title></head><body>'
       '<h1>스마트테크코리아 2026 — Exhibitor Directory</h1>'
       '<p>AI &amp; Big Data Show / Robot Tech Show / Smart Factory Show / Retail &amp; Logis Tech Show / Security Tech Show — pages 1-3 (150 of ~550 exhibitors).</p>'
       + '<hr/>'.join(pages) + '</body></html>')
pathlib.Path('tests/fixtures/events/smarttech_korea_2026.html').write_text(out, encoding='utf-8')
PY
```

## Capture pattern for other JS-rendered sites

If a site doesn't expose a paginated AJAX endpoint, fall back to operator-assisted Save As:

1. Open the listing page in a browser
2. Scroll until all rows have loaded
3. `Ctrl+S` → "Webpage, Complete"
4. Drop the `.html` into this directory
5. Run `event-intel build-event --html-file <path> ...`
