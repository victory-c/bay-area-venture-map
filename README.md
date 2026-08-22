# Sand Hill Road VCs

A founder-facing reference for the Sand Hill Road / Menlo Park venture cluster.
Interactive Leaflet map + sortable, filterable table on a single static page.
Filter firms by AUM, stage, sector, and check size; click any pin or row for
partner contacts and a public sample of recent investments.

This repo has two pieces:

- `scraper/` — a tiny Python pipeline that reads `data/firms.seed.yaml`,
  optionally enriches it from public sources (SEC IAPD for AUM, OSM Nominatim
  for geocoding), and writes `data/firms.json`.
- `site/` — a static page (vanilla HTML + Alpine.js + Leaflet, no build step)
  that consumes `firms.json`. Serve the folder with any static server; the
  page fetches `firms.json` at runtime, so opening `site/index.html` straight
  off the filesystem is blocked by the browser and the page will tell you so.

## Usage

```bash
# 1. Install Python deps (uv).
uv sync

# 2. Build firms.json from the curated seed (no network needed).
uv run python -m scraper.build

# 3. Serve the static site locally.
cd site && python -m http.server 8000
# → open http://localhost:8000
```

Refresh AUM from SEC Form ADV (live network):

```bash
uv run python -m scraper.build --enrich edgar
```

Refresh coordinates via OpenStreetMap Nominatim (live network, slow — 1 req/s):

```bash
uv run python -m scraper.build --enrich geocode
```

Rebuild a single firm in place during development:

```bash
uv run python -m scraper.build --firm sequoia --enrich edgar
```

## Tests

```bash
uv run pytest
```

The EDGAR and geocode tests use mocked `httpx.MockTransport` responses, so they
don't hit the network.

## Data caveats

This is a curated reference, not a financial database. Specifically:

- **AUM is "regulatory AUM"** from SEC Form ADV Item 5.F1 when populated via
  the `edgar` enricher. Otherwise the seed value is a press estimate, marked
  as such in `aum_source`. Regulatory AUM differs from fund size or dry powder.
- **Some firms aren't SEC-registered investment advisers** (foreign-domiciled
  funds, very small ERAs); their AUM stays as the seed estimate.
- **"Recent portfolio sample" is partial**, not exhaustive. Crunchbase /
  PitchBook would give more coverage, but their ToS prohibits scraping and
  their APIs cost thousands per year.
- **Partner LinkedIn / X links are public profiles**, not scraped data. We
  link out — no scraping of those platforms.
- **"Sand Hill Road" is interpreted as the cluster** (Sand Hill Rd plus the
  immediate Menlo Park / Palo Alto VC corridor). The strict
  `on_sand_hill_road: true` filter narrows to firms with a literal Sand Hill
  Rd address.

When using this for actual fundraising, **always cross-check against the
firm's most recent Form ADV** at <https://adviserinfo.sec.gov> before relying
on AUM figures.

## Adding a firm

1. Append an entry to `data/firms.seed.yaml` following the existing schema.
2. Add hand-verified `(lat, lng)` to `DEFAULT_COORDINATES` in
   `scraper/build.py`, or run `--enrich geocode` once to populate the cache.
3. Re-run `uv run python -m scraper.build` and reload the site.

## License

Code: MIT. Data: best-effort compilation from public sources; no warranty of
accuracy, fitness for any purpose, or non-staleness. Not investment advice.
