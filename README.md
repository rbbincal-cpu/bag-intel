# Bag Intel — luxury resale competitor monitor

Fully automated daily monitoring of Philippine luxury-bag resale stores, with a
GitHub Pages dashboard and daily Excel exports. Runs at **6:00 AM Asia/Manila**
every day via GitHub Actions with zero manual work.

## How it works

All five monitored stores run on Shopify, so data comes from each store's public
`/products.json` API (no HTML scraping). Each daily run:

1. **Scrapes** every store's full catalog (polite: 1.5s delay between pages,
   identified user agent, robots.txt check, 3 retries with backoff).
2. **Snapshots** into `data/market.db` (SQLite, committed to the repo).
3. **Detects sales** — an item is SOLD only if it (a) flips to sold-out, or
   (b) disappears for 3+ consecutive daily runs without being re-listed.
   Sold price = last known price; days-to-sell = first seen → sold date.
4. **Detects re-uploads** by normalized title + perceptual image hash, so
   re-listed bags aren't double-counted (the original listing's first-seen
   date carries over).
5. **Logs price changes** (every markdown/markup with date, amount, %).
6. **Regenerates** `site/data.json`, deploys the dashboard to GitHub Pages,
   and writes `exports/bag-intel-YYYY-MM-DD.xlsx` (last 30 kept).
7. On failure: opens/updates a GitHub issue labeled `scrape-failure`
   (GitHub emails you automatically).

## Dashboard

- **Overview** — all stores side by side vs Purse Maison (★): inventory
  value/count, sold MTD, new uploads MTD, avg ticket, brand mix, stock aging
  (60/90 days), markdowns, avg days-to-sell, plus trend charts and upload
  cadence by weekday.
- **Competitors** — per-store detail: KPI cards, sold-by-brand/model, recent
  sales, price drops, full live listing table with color/hardware/leather.
- **Hero Models** — Birkin 25/30, Kelly 25/28/Mini, Constance 18/24, Chanel
  Classic Flap S/M/Jumbo: listings per store with attributes, sold MTD,
  avg sold price, avg days-to-sell, lowest/highest ask across the market.

## Setup (one time)

1. Create a **private** repo and push this folder to `main`
   (private Pages requires GitHub Pro).
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. Repo **Settings → Actions → General → Workflow permissions:
   Read and write permissions**.
4. Run the workflow once manually: **Actions → Daily market scrape →
   Run workflow**. The schedule then runs daily at 6AM Manila.

Local run (optional):

```bash
pip install -r requirements.txt
python scraper/scrape.py        # fetch + ingest
python scraper/analyze.py       # -> site/data.json
python scraper/export_excel.py  # -> exports/*.xlsx
# open site/index.html in a browser (serve the folder: python -m http.server -d site)
```

## Add / remove a competitor

Edit `config.yaml` → `sites:` list. Each entry needs:

```yaml
- key: short_id          # unique, no spaces
  name: Display Name
  base_url: https://thestore.com
  is_mine: false
  brand_source: vendor   # vendor | tags | title — how to detect the brand
```

Pick `brand_source` by checking `https://thestore.com/products.json?limit=2`:
if `vendor` holds the real brand use `vendor`; if brands are in `tags` use
`tags`; otherwise `title`. The site must be Shopify (products.json must work).
Removing a site = delete its entry (history stays in the DB).

## Adjust hero models

Edit `config.yaml` → `hero_models:`. Keywords are case-insensitive regexes
matched against the normalized title (lowercased, accents/punctuation
stripped), so cover variants, e.g. `['birkin\s*25\b', '\bb25\b', 'birkin25']`.

## Notes & caveats

- Stores that delete sold listings (Orange Box, Bag Hub, Shop with K) rely on
  the 3-day disappearance rule, so their first sales appear after ~4 daily runs.
- Items already sold-out when tracking began are excluded from sold metrics
  (sold date unknown). Trends get richer the longer it runs.
- ₱0 placeholder listings are excluded from value metrics. "HOLD |" titles are
  tracked as reserved, kept in inventory value.
- Sold detection from public data is inference: bags removed for other reasons
  (consignor pull-out, off-platform sale, hidden) count as "sold". Treat
  sold-MTD as an upper-bound signal, not audited revenue.
- The dashboard has `noindex` meta tags, but anyone with the Pages URL can view
  it; the repo (and DB/exports) stay private.

## Layout

```
config.yaml            sites + hero models + scrape politeness settings
scraper/common.py      schema, normalization, brand/attribute extraction
scraper/scrape.py      daily fetch + ingest + sold/relist/price logic
scraper/analyze.py     metrics -> site/data.json
scraper/export_excel.py
site/                  static dashboard (deployed to Pages as-is)
data/market.db         SQLite history (committed daily)
exports/               daily Excel files (last 30)
.github/workflows/daily.yml
```
