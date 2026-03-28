# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a PM take-home case study for Allium, building an **Institutional Prediction Markets Intelligence** product. The repo contains both the product strategy documents and a working prototype dashboard.

The product transforms raw prediction market data from Polymarket, Kalshi, and Jupiter into institutional-grade intelligence — probability distributions, cross-venue comparison, market quality scoring, and wallet analysis — delivered via UI, API, and Snowflake data share.

## Repository Structure

- `project_doc.md` — Product strategy document (Sections 1-7: problem decomposition, personas, feature brief, constraints, engineering questions, success criteria, GTM)
- `technical_implementation.md` — Technical spec with Allium data schemas, gap analysis, architecture, and MVP prototype plan
- `prediction-markets-prototype/` — Working prototype dashboard
  - **`dashboard.html`** — Full two-page dashboard (Market Explorer + BTC Deep Dive). This is the main file.
  - **`fetch_all_data.py`** — Unified data fetcher that queries Allium API and produces both JSON files below
  - `explorer_data.json` — All active markets with thematic tags, volumes, and prices (Page 1)
  - `btc_deep_dive.json` — BTC distributions, spot prices, and wallet data (Page 2)
  - `hero-chart.html` — Standalone BTC hero chart (earlier iteration, kept for reference)
  - `fetch_data.py` — Earlier single-purpose BTC data fetcher (superseded by fetch_all_data.py)
  - `index.html` — Earliest prototype with CDF/PDF charts (kept for reference)

## Running the Prototype

```bash
cd prediction-markets-prototype

# Fetch fresh data from Allium API (produces explorer_data.json + btc_deep_dive.json)
python3 fetch_all_data.py

# Serve locally (fetch won't work from file://)
python3 -c "
import http.server, socketserver, os
os.chdir('/Users/teaji/Work/Allium_Case_Study/prediction-markets-prototype')
with socketserver.TCPServer(('127.0.0.1', 8090), http.server.SimpleHTTPRequestHandler) as h:
    print('http://127.0.0.1:8090/dashboard.html')
    h.serve_forever()
"
```

**Dependencies:** `pip3 install requests`

## Allium API Access

- **Endpoint:** `POST https://api.allium.so/api/v1/explorer/queries` (create), then `POST .../queries/{id}/run` (execute)
- **Auth:** `X-API-Key` header
- **Key tables:**
  - `crosschain.predictions.markets` — Normalized markets across venues
  - `crosschain.predictions.trades` — Trade-level data with wallet addresses (Polymarket/Jupiter only)
  - `polygon.wallet_features.wallet_360` — 100+ wallet attributes
  - `crosschain.rwa.prices` — Tokenized equity prices
- **Gotcha:** Cast `TIMESTAMP_TZ` columns to `::timestamp_ntz` to avoid Parquet export errors
- **Gotcha:** Kalshi `token_price` is NULL in `crosschain.predictions.markets` — prices must be pulled from latest trades via `crosschain.predictions.trades`
- **Gotcha:** Large queries (5000+ rows) can 503/524. Use retries and keep limits at 2000 or below.
- **Venue differences:** Polymarket has maker/taker wallet addresses; Kalshi has none (centralized); Jupiter has taker only
- **Jupiter data gap:** As of ~March 11 2026, all recent Jupiter trades in `solana.predictions.trades` have NULL ticker, user_address, prices, and action. Jupiter also absent from `crosschain.predictions.markets`. Dashboard code to show Jupiter volume on Kalshi markets is in place but won't display until Allium fixes ingestion.

## Key Design Decisions

- **Color scheme for distributions:** Red = below current spot price, green = above. Color intensity = probability. Discrete $2K rectangular bands, not continuous gradients.
- **Volume-weighted consensus:** When multiple market types (above/below, price range) or venues price the same outcome, we weight by volume to produce a single probability per band.
- **Spot price line ends at today:** Distributions are placed on future expiry dates only — no overlap between historical prices and prediction distributions.
- **Thematic mapping:** Allium's 9 categories (politics, crypto, sports, etc.) are remapped to institutional themes (Energies, Rates, Equities, Crypto, Geopolitics) via keyword matching on question/description fields.
- **Wallet analysis is Polymarket-centric:** Only Polymarket provides maker/taker addresses for participant-level analysis.

## Dashboard (dashboard.html)

Two-page SPA with tab navigation. Loads `explorer_data.json` and `btc_deep_dive.json` via fetch.

**Page 1: Market Explorer**
- Filterable table of active markets across Polymarket and Kalshi
- Theme filters (Energies, Rates, Equities, Crypto, Geopolitics), venue filters, search
- Insight badges: Contested (40-60% prob), High Signal (high volume + traders), Low Liquidity, Resolving Soon
- Quality column (unique trader count), days-to-resolution countdown
- Sortable by any column

**Page 2: BTC Deep Dive**
- Hero chart: Canvas-drawn BTC spot price + distribution bands per expiry (red below spot, green above)
- Band detail table with date tabs and per-source attribution
- Source comparison: Chart.js grouped bar chart showing Polymarket vs Kalshi per price band
- Top wallets panel: largest BTC prediction market participants with other-market activity

**Known issue:** Hero chart canvas requires the page to be visible to size correctly — `drawHeroChart()` is called on tab switch via setTimeout.
