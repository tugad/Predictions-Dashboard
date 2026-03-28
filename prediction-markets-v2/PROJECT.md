# Prediction Markets Intelligence — V2 Project Specification

## 1. Context and Motivation

This is a rebuild of the market explorer page for the Allium Prediction Markets Intelligence prototype. The original prototype (in `../prediction-markets-prototype/`) fetches all data from Allium's SQL Explorer API. Through testing, we discovered:

1. **Allium has significant coverage gaps.** Polymarket has ~54K active markets; Allium's crosschain tables return ~10K rows max, with many markets missing entirely. Kalshi has ~10K+ open markets; Allium captures most but with stale event tickers.

2. **Allium's volumes are correct but native APIs report differently.** Polymarket's Gamma API double-counts volume (each trade fires two `OrderFilled` events — maker + taker side). This was documented by Paradigm in December 2025. Allium correctly reports one-sided volume. Kalshi's API reports volume in contracts (`volume_fp` = total shares traded), while Allium reports in USD (`usd_amount` = shares × price). These are the same underlying data in different units — verified by exact match: Allium `num_shares` sum == Kalshi `volume_fp` for every tested market.

3. **Allium's unique value is wallet/participant data**, not market discovery. Only Allium can tell you how many unique traders are in a market, who the top wallets are, and what else those wallets trade. Polymarket and Kalshi's public APIs don't expose this.

4. **Keyword-based theme tagging fails badly.** Matching "bond" caught Venezuelan custody markets. Matching "fed" caught unrelated text. LLM-based classification with current macro context produces dramatically better results.

**Decision:** Use native venue APIs (Polymarket Gamma, Kalshi REST) for market discovery, prices, and volumes. Use Allium only for on-demand enrichment (wallet data, trader counts) when a user clicks into a specific market. Use LLM-based classification with per-theme context dictionaries for thematic tagging.

---

## 2. Architecture Overview

```
                    ┌─────────────────────┐
                    │   Web Search         │
                    │   (macro context)    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  theme_contexts.json │  ← Updated daily or per-fetch
                    └──────────┬──────────┘
                               │
┌──────────────┐               │              ┌──────────────┐
│  Polymarket  │               │              │    Kalshi     │
│  Gamma API   │               │              │   REST API   │
└──────┬───────┘               │              └──────┬───────┘
       │                       │                     │
       ▼                       ▼                     ▼
┌──────────────────────────────────────────────────────────┐
│                    fetch_markets.py                        │
│                                                            │
│  1. Fetch all active markets from both venues              │
│  2. Normalize into common schema                           │
│  3. Filter noise (micro markets, expired, low liquidity)   │
│  4. Compute consensus prices for range markets             │
│  5. Load theme_overrides.json for cached tags              │
│  6. Identify new/untagged markets                          │
│  7. → Writes markets_raw.json (all markets, pre-theme)     │
└──────────────────────────┬───────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│                   extract_themes.py                        │
│                                                            │
│  1. Loads markets_raw.json + theme_contexts.json           │
│  2. Checks theme_overrides.json for cached tags            │
│  3. Batches new markets (100 per batch)                    │
│  4. Sends to Claude agents for classification              │
│  5. Merges results into theme_overrides.json               │
│  6. Applies themes + sub-tags to all markets               │
│  7. → Writes explorer_data.json (final output)             │
└──────────────────────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────┐
│                     dashboard.html                         │
│                                                            │
│  Loads: explorer_data.json + theme_contexts.json           │
│  On market click: calls enrich_market.py or inline fetch   │
└──────────────────────────────────────────────────────────┘
```

The BTC Deep Dive (Page 2) is unchanged from v1 — it continues to use `fetch_btc_data.py` (carried over from `fetch_all_data.py`'s BTC section) and Allium's trade-level data for distributions and wallet analysis.

---

## 3. File Structure

```
prediction-markets-v2/
├── PROJECT.md              ← This document
├── dashboard.html          ← Two-page SPA (explorer + BTC deep dive)
├── fetch_markets.py        ← Fetches from Polymarket + Kalshi, normalizes, writes markets_raw.json
├── extract_themes.py       ← LLM theme classification, writes explorer_data.json
├── fetch_btc_data.py       ← BTC deep dive data from Allium (carried from v1)
├── theme_contexts.json     ← Per-theme context paragraphs + sub-filter definitions
├── theme_overrides.json    ← Cached question → themes mapping (persisted across runs)
├── markets_raw.json        ← Intermediate: all markets before theme tagging
├── explorer_data.json      ← Final output for dashboard Page 1
└── btc_deep_dive.json      ← Output for dashboard Page 2 (from fetch_btc_data.py)
```

### Run Order

```bash
# 1. Fetch markets from native APIs
python3 fetch_markets.py

# 2. Extract themes (requires Claude — run via Claude Code agent)
python3 extract_themes.py

# 3. Fetch BTC deep dive data from Allium
python3 fetch_btc_data.py

# 4. Serve dashboard
python3 -m http.server 8091
```

Steps 1 and 3 can run in parallel. Step 2 depends on step 1's output.

---

## 4. Data Pipeline: fetch_markets.py

### 4.1 Polymarket Fetch

**Endpoint:** `GET https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&offset={n}`

Paginate through all active events. Each event contains a `markets` array. For each market, extract:

| Field | Source | Notes |
|-------|--------|-------|
| question | `market.question` | Primary identifier for deduplication and theme matching |
| description | `market.description` | Used as secondary input for theme classification |
| price | `market.outcomePrices` | JSON string like `["0.704","0.296"]`. Parse first element (Yes price). |
| volume_24h | `market.volume24hr` / 2 | **Divide by 2** — Polymarket double-counts (maker + taker OrderFilled events) |
| volume_7d | `market.volume1wk` / 2 | Same correction |
| volume_30d | `market.volume1mo` / 2 | Same correction |
| liquidity | `market.liquidity` | No correction needed |
| end_date | `market.endDate` | ISO 8601 |
| venue | `"polymarket"` | Hardcoded |
| native_tags | `event.tags[].label` | Polymarket's own tags — useful as classification hints |
| slug | `market.slug` | For linking back to Polymarket |
| condition_id | `market.conditionId` | Unique market identifier on Polymarket |

**Filters applied during fetch:**
- Skip markets where `question` contains "5 min", "15 min", or "Up or Down" (micro-trading markets)
- Skip markets where `endDate` < today (expired but still marked active)
- Skip markets where `liquidity` < 100 (effectively dead markets)

**Deduplication:** By `(venue, question)` tuple. Some markets appear in multiple events.

### 4.2 Kalshi Fetch

**Endpoint:** `GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&cursor={c}`

Paginate through all open markets using cursor-based pagination. Extract:

| Field | Source | Notes |
|-------|--------|-------|
| question | `market.title` | Kalshi uses "title" not "question" |
| description | `market.rules_primary` | Resolution rules |
| price | `market.last_price_dollars` | Already in dollars, 0-1 scale |
| price_bid | `market.yes_bid_dollars` | Best bid |
| price_ask | `market.yes_ask_dollars` | Best ask |
| volume_24h | `market.volume_24h_fp * market.last_price_dollars` | **volume_24h_fp is in contracts** — multiply by price to get USD estimate |
| volume_total | `market.volume_fp * market.last_price_dollars` | Same conversion. This is an approximation since price varies over time. |
| liquidity | `market.liquidity_dollars` | Already in dollars |
| end_date | `market.close_time` | ISO 8601 |
| venue | `"kalshi"` | Hardcoded |
| event_ticker | `market.event_ticker` | For grouping related markets |
| market_ticker | `market.ticker` | For individual market identification |

**Volume conversion note:** Multiplying contracts × last_price is an approximation. The true USD volume would require summing (shares × price) for each historical trade. For the explorer, this approximation is acceptable — it gives the right order of magnitude and relative ranking. Allium has the exact USD volumes if needed for enrichment.

**Filters:**
- Skip markets where `close_time` < today
- Skip markets where `volume_fp` == 0 AND `liquidity_dollars` == 0 (no activity)

### 4.3 Consensus Price Computation

Some Kalshi markets are "price on date" events with many outcome bands (e.g., "Bitcoin price range on Mar 28" has 40+ bands at different strike prices). These show up as individual markets in the Kalshi API, grouped by `event_ticker`.

**Detection:** Multiple markets share the same `event_ticker` and their `ticker` contains `-B` (band) or `-T` (threshold) suffixes.

**Computation:**
1. Group markets by `event_ticker`
2. For each group, extract the strike price from the ticker suffix (e.g., `KXBTC-26MAR2714-B65750` → $65,750)
3. Use `last_price_dollars` as the probability for each band
4. Compute `consensus_price = sum(strike × probability) / sum(probability)`
5. Record `peak_band` (highest probability strike) and `peak_prob`

**Spot prices** for delta calculation are fetched via a web search at the start of the run, or hardcoded with a timestamp. The delta is `(consensus_price - spot_price) / spot_price`.

Assets to track spot prices for: BTC, ETH, SOL, BNB, S&P 500, Nasdaq-100.

**In the output JSON**, the parent event gets `is_price_market: true`, `consensus_price`, `peak_band`, `peak_prob`, `spot_price`, and the individual band markets are excluded from the explorer (they'd clutter the table). Only the parent event row is shown.

### 4.4 Output: markets_raw.json

```json
{
  "generated_at": "2026-03-27T18:00:00Z",
  "markets": [
    {
      "venue": "polymarket",
      "question": "US forces enter Iran by March 31?",
      "description": "This market will resolve to Yes if...",
      "price": 0.165,
      "volume_24h": 1530000,
      "volume_7d": 6114000,
      "liquidity": 250000,
      "end_date": "2026-03-31",
      "native_tags": ["Iran", "Geopolitics", "Middle East"],
      "slug": "us-forces-enter-iran-march-31",
      "event_ticker": null,
      "market_ticker": null,
      "is_price_market": false,
      "consensus_price": null,
      "spot_price": null,
      "themes": null,
      "sub_tags": null
    },
    {
      "venue": "kalshi",
      "question": "Bitcoin price range on Mar 28, 2026 at 1am EDT?",
      "description": "...",
      "price": null,
      "volume_24h": 52000,
      "volume_7d": null,
      "liquidity": 15000,
      "end_date": "2026-03-28",
      "native_tags": [],
      "slug": null,
      "event_ticker": "KXBTC-26MAR2801",
      "market_ticker": "KXBTC-26MAR2801-B65750",
      "is_price_market": true,
      "consensus_price": 67022,
      "peak_band": 65750,
      "peak_prob": 0.43,
      "spot_price": 68878,
      "themes": null,
      "sub_tags": null
    }
  ]
}
```

---

## 5. Theme Extraction: extract_themes.py

### 5.1 Theme Contexts

Stored in `theme_contexts.json`. Updated daily (or per-fetch) via web search for current market conditions. Structure:

```json
{
  "updated_at": "2026-03-27",
  "themes": {
    "Energies": {
      "context": "Iran-Israel war closed Strait of Hormuz on March 4, stranding 20% of global oil supply. Brent at $120/bbl, largest supply disruption in IEA history. Qatar LNG force majeure. Venezuela US strikes threatening additional supply. European gas storage critically low at 46bcm vs 60bcm last year. Natural gas prices spiking in Europe and Asia.",
      "sub_filters": ["Oil", "Natural Gas", "Nuclear", "Shipping", "Renewables"],
      "sub_keywords": {
        "Oil": ["oil", "crude", "brent", "wti", "opec", "petroleum", "barrel", "refinery"],
        "Natural Gas": ["natural gas", "lng", "gas storage", "gas prices", "pipeline"],
        "Nuclear": ["nuclear", "uranium", "reactor", "atomic"],
        "Shipping": ["shipping", "freight", "tanker", "strait", "hormuz", "route", "port"],
        "Renewables": ["solar", "wind", "renewable", "clean energy", "ev", "battery"]
      }
    },
    "Rates": {
      "context": "Fed held at 3.5-3.75% on March 18. Inflation revised up to 2.7% on oil shock. Dot plot shows one cut this year, one in 2027. Stagflation risk rising. ECB pausing amid energy cost uncertainty. BOJ holding. Treasury yields elevated. Credit spreads widening.",
      "sub_filters": ["Fed/FOMC", "Inflation/CPI", "Treasuries", "Central Banks"],
      "sub_keywords": {
        "Fed/FOMC": ["fed", "fomc", "federal reserve", "powell", "rate cut", "rate hike", "funds rate"],
        "Inflation/CPI": ["inflation", "cpi", "ppi", "deflation", "prices", "cost of living"],
        "Treasuries": ["treasury", "bond", "yield", "note", "t-bill", "fixed income", "credit spread"],
        "Central Banks": ["ecb", "boj", "boe", "rba", "central bank", "monetary policy", "pboc", "rbnz"]
      }
    },
    "Equities": {
      "context": "S&P down ~8% from February highs on war uncertainty. AI capex shifting from buildout to adoption — ROI pressure on Big Tech. Liftoff Mobile IPO upcoming. Energy stocks rallying, tech and consumer discretionary selling off. Earnings season approaching. Small-cap underperformance. Market leadership broadening away from mega-caps.",
      "sub_filters": ["Tech/AI", "IPOs", "Indices", "Single Stocks"],
      "sub_keywords": {
        "Tech/AI": ["ai", "artificial intelligence", "gpt", "openai", "nvidia", "semiconductor", "chip", "data center", "cloud"],
        "IPOs": ["ipo", "listing", "public offering", "debut", "direct listing"],
        "Indices": ["s&p", "sp500", "nasdaq", "dow", "russell", "index", "indices"],
        "Single Stocks": ["tesla", "tsla", "meta", "apple", "aapl", "amazon", "amzn", "google", "googl", "microsoft", "msft", "nvidia", "nvda", "netflix", "nflx", "palantir", "pltr"]
      }
    },
    "Crypto": {
      "context": "BTC at $68K, down from $75K. $15B options expiry this week. Spot ETF outflows accelerating on risk-off sentiment. Trump crypto capital gains tax proposal in play. Stablecoin regulation expected. MegaETH launch imminent. Ethena and DeFi protocol tokens under pressure.",
      "sub_filters": ["BTC", "ETH", "SOL", "DeFi/Tokens", "Stablecoins", "Regulation"],
      "sub_keywords": {
        "BTC": ["bitcoin", "btc"],
        "ETH": ["ethereum", "eth"],
        "SOL": ["solana", "sol"],
        "DeFi/Tokens": ["defi", "token", "airdrop", "nft", "protocol", "dex", "uniswap", "megaeth", "ethena", "xrp", "dogecoin", "memecoin", "altcoin"],
        "Stablecoins": ["stablecoin", "usdt", "usdc", "tether", "circle", "dai"],
        "Regulation": ["crypto regulation", "sec crypto", "capital gains tax crypto", "crypto ban", "crypto policy"]
      }
    },
    "Geopolitics": {
      "context": "US-Israel strikes on Iran ongoing. Strait of Hormuz closed. Russia-Ukraine peace referendum and ceasefire discussions. US military strikes on Venezuela and Colombia. NATO cohesion under pressure — Hormuz deployment discussed. Israel-Hamas ceasefire fragile. Israel-Syria normalization talks. Vietnam leadership succession. China-Taiwan tensions.",
      "sub_filters": ["Iran/Middle East", "Russia/Ukraine", "NATO", "China/Taiwan", "Latin America"],
      "sub_keywords": {
        "Iran/Middle East": ["iran", "israel", "hamas", "hezbollah", "gaza", "syria", "hormuz", "lebanon", "middle east", "ceasefire"],
        "Russia/Ukraine": ["russia", "ukraine", "putin", "zelenskyy", "crimea", "donbas", "lyman", "kupiansk"],
        "NATO": ["nato", "alliance", "troops", "military", "defense"],
        "China/Taiwan": ["china", "taiwan", "xi", "beijing", "south china sea", "pla"],
        "Latin America": ["venezuela", "maduro", "colombia", "brazil", "mexico", "machado"]
      }
    },
    "Commodities": {
      "context": "Gold rallying as safe haven amid war and stagflation fears. Silver following gold. Agricultural commodities disrupted by shipping route changes around Horn of Africa. Critical minerals in focus — JD Vance Critical Minerals Ministerial. Base metals mixed on China demand uncertainty.",
      "sub_filters": ["Gold", "Silver", "Agriculture", "Critical Minerals"],
      "sub_keywords": {
        "Gold": ["gold", "bullion", "xau", "comex gold"],
        "Silver": ["silver", "xag", "comex silver"],
        "Agriculture": ["agriculture", "wheat", "corn", "soybean", "crop", "food", "grain", "fertilizer"],
        "Critical Minerals": ["lithium", "cobalt", "rare earth", "critical mineral", "nickel", "copper", "mining"]
      }
    },
    "Macro": {
      "context": "China GDP growth uncertain — wide range of outcomes being priced from sub-1% to 6%+. US unemployment expected to rise, watched as Fed reaction function input. Global recession risk elevated from oil shock + tariffs. Stagflation narrative dominant. US fiscal deficit concerns. Dollar strengthening on risk-off. IMF revising global growth downward.",
      "sub_filters": ["GDP", "Unemployment/Jobs", "Recession", "Fiscal/Dollar"],
      "sub_keywords": {
        "GDP": ["gdp", "growth", "economic growth", "output"],
        "Unemployment/Jobs": ["unemployment", "jobs", "nonfarm", "labor", "employment", "jobless"],
        "Recession": ["recession", "downturn", "slowdown", "contraction", "stagflation"],
        "Fiscal/Dollar": ["fiscal", "deficit", "debt", "dollar", "dxy", "budget", "spending", "forex", "exchange rate"]
      }
    },
    "Politics": {
      "context": "US midterm positioning underway. Trump presidency stability markets active. Congressional dynamics — Speaker Johnson. State-level elections gaining attention. Hungary election upcoming. Regulatory policy shifts — SEC, crypto regulation, tariff implementation. Immigration enforcement actions. Venezuela regime change dynamics.",
      "sub_filters": ["US Federal", "US Midterms", "Global Elections", "Regulation"],
      "sub_keywords": {
        "US Federal": ["trump", "president", "white house", "congress", "senate", "speaker", "cabinet", "supreme court", "scotus"],
        "US Midterms": ["midterm", "governor", "primary", "ballot", "state election", "house race", "senate race"],
        "Global Elections": ["election", "vote", "referendum", "parliament", "prime minister", "coalition"],
        "Regulation": ["sec", "regulation", "policy", "tariff", "tax", "legislation", "bill", "law", "fda", "ftc", "doj"]
      }
    }
  }
}
```

### 5.2 Classification Process

1. Load `markets_raw.json` and `theme_overrides.json`
2. For each market, check if its `question` exists in `theme_overrides.json`. If yes, use cached themes and skip.
3. Collect all uncached markets into a list.
4. Split into batches of ~100 markets.
5. For each batch, construct a prompt that includes:
   - All 8 theme context paragraphs from `theme_contexts.json`
   - The list of market questions (numbered)
   - Instructions to assign one or more themes per market, or "Other" if not institutionally relevant
6. Send batches to Claude agents in parallel (up to 4 concurrent).
7. Parse responses (Python dict literal mapping index → theme list).
8. Merge into `theme_overrides.json`.

**Classification prompt template:**

```
You are tagging prediction markets for institutional portfolio managers.
Assign one or more themes from the list below based on relevance to that
sector. If a market is not relevant to institutional portfolio managers
(sports, weather, entertainment, baby names, social media, etc.), tag it
as "Other".

A market CAN have multiple themes when it's relevant to more than one.
For example, Iran conflict markets are both "Geopolitics" and "Energies"
because the Hormuz closure directly impacts oil supply.

=== THEME CONTEXTS (current as of {date}) ===

ENERGIES: {energies_context}

RATES: {rates_context}

EQUITIES: {equities_context}

CRYPTO: {crypto_context}

GEOPOLITICS: {geopolitics_context}

COMMODITIES: {commodities_context}

MACRO: {macro_context}

POLITICS: {politics_context}

=== MARKETS TO CLASSIFY ===

{numbered_market_list}

=== OUTPUT ===

Output ONLY a Python dictionary mapping index number to a list of theme
strings. No explanation needed.
```

### 5.3 Sub-Tag Assignment

After primary theme classification, sub-tags are assigned using keyword matching within each theme. This is simpler than the primary classification because it operates on already-correctly-themed markets.

For a market tagged `["Geopolitics", "Energies"]` with question "US forces enter Iran by March 31?":
- Geopolitics sub-tags: check question against each sub-filter's keywords → matches "Iran/Middle East"
- Energies sub-tags: check question against each sub-filter's keywords → matches "Oil" (via "Iran" appearing in the Oil context... actually no, "Iran" is not an Oil keyword)

**Important:** Sub-tag keywords are intentionally narrow. A market might be tagged "Energies" by the LLM (because the LLM understands Iran → oil disruption) but not match any Energies sub-filter keyword (because "Iran" is not literally an oil keyword). This is correct behavior — the market appears under Energies but is not filtered when a specific sub-filter like "Oil" is selected. It only disappears if a sub-filter IS actively selected and it doesn't match. When no sub-filter is selected (default), all markets in the theme are shown.

### 5.4 Output: explorer_data.json

```json
{
  "generated_at": "2026-03-27T18:05:00Z",
  "spot_prices": {
    "BTC": 68878, "ETH": 2071, "SOL": 87,
    "BNB": 620, "SP500": 5700, "NDX": 19800
  },
  "markets": [
    {
      "venue": "polymarket",
      "question": "US forces enter Iran by March 31?",
      "description": "...",
      "price": 0.165,
      "volume_24h": 1530000,
      "volume_7d": 6114000,
      "liquidity": 250000,
      "end_date": "2026-03-31",
      "themes": ["Geopolitics", "Energies"],
      "sub_tags": ["Iran/Middle East"],
      "slug": "us-forces-enter-iran-march-31",
      "is_price_market": false,
      "consensus_price": null,
      "spot_price": null
    }
  ]
}
```

### 5.5 Edge Cases

**Market appears in both venues:** This shouldn't happen often (Polymarket and Kalshi have different market structures), but if the same question appears on both, keep both entries. The venue filter lets users distinguish them.

**Theme has 0 markets:** The theme button still appears in the UI but shows "(0)" count and is not clickable / greyed out. The context paragraph still displays if somehow selected.

**New markets on subsequent runs:** Only new questions get sent to the LLM. The `theme_overrides.json` file grows over time. Markets that were previously classified and have since expired naturally stop appearing (they're filtered by `end_date`), but their overrides remain in the file for potential reuse if the market reopens.

**LLM classification failure:** If an agent returns malformed output, log the error and tag those markets as "Other" as a fallback. Do not block the entire pipeline.

**Empty description:** Many Polymarket markets have empty or null descriptions. Classification should work from question text alone — the description is supplementary.

---

## 5.6 Theme Intelligence Summaries

Each theme gets a set of natural language bullet points summarizing the key signals from its High and Med quality markets. These are displayed in a card between the context panel and the sub-filters when a theme is selected.

### Purpose

A portfolio manager clicking "Energies" shouldn't have to scan 200 rows to understand the signal. The summary distills dozens of individual markets into 3-8 actionable bullet points — the same synthesis a research analyst would produce manually.

### Generation Flow

Runs as a post-processing step after theme extraction, during `extract_themes.py` (or a separate `generate_summaries.py` script).

1. For each theme, collect all High + Med quality markets (volume_7d >= $50K).
2. Group related markets:
   - Same `event_ticker` (Kalshi) → same event
   - Same question prefix / similar wording (Polymarket) → same topic
   - Multiple date variants of the same question (e.g., "X by March 31", "X by April 30", "X by June 30") → single topic with timeline
3. For each group, extract:
   - Representative probability (highest-volume market in the group)
   - Combined volume across all markets in the group
   - Number of corroborating markets
   - Date range (earliest and latest resolution dates)
4. Send grouped data to a Claude agent per theme with this prompt structure:

```
You are a research analyst writing a market intelligence briefing for
institutional portfolio managers.

Given the following prediction market data for the {theme} sector,
produce 3-8 bullet points summarizing the key signals. Each bullet should:
- State what the market is pricing in plain language
- Include the probability as a percentage
- Include a confidence indicator: HIGH (multiple markets, >$500K combined vol),
  MEDIUM ($100K-$500K or single high-vol market), or LOW (<$100K)
- If there's a timeline (same event with different dates), mention the
  probability curve across dates

Do NOT just list markets. Synthesize — combine related signals into single
insights. Lead with the most actionable/surprising signals.

=== MARKET DATA ===
{grouped_market_data}
```

5. Store results in `explorer_data.json` under a `theme_summaries` key:

```json
{
  "theme_summaries": {
    "Energies": {
      "generated_at": "2026-03-27T18:30:00Z",
      "bullets": [
        {
          "text": "Iran ceasefire by end of March priced at only 3%",
          "probability": 0.03,
          "confidence": "HIGH",
          "volume": 10500000,
          "market_count": 8
        },
        {
          "text": "Crude oil above $100 by end of March at 70%, rising to 85% by June",
          "probability": 0.70,
          "confidence": "HIGH",
          "volume": 4200000,
          "market_count": 4
        }
      ]
    }
  }
}
```

### Display

Shown in a card below the context panel when a theme is selected. Each bullet is a single line with:
- The text in white
- Probability in bold
- Confidence badge (colored: green=HIGH, yellow=MEDIUM, gray=LOW)

```
┌──────────────────────────────────────────────────────────────┐
│ MARKET INTELLIGENCE — ENERGIES                               │
│                                                              │
│ - Iran ceasefire by March priced at only 3%          [HIGH]  │
│ - Crude oil $100+ by March at 70%, 85% by June      [HIGH]  │
│ - US forces in Iran by April — 58%                   [HIGH]  │
│ - Strait of Hormuz reopening — no markets pricing    [——]    │
│   above 10% before June                                      │
│ - Kharg Island under Iranian control — 94%           [MED]   │
│                                                              │
│                             Generated Mar 27                 │
└──────────────────────────────────────────────────────────────┘
```

When "All" is selected, no summary is shown (too broad to summarize meaningfully).

### Edge Cases

- **Theme has <3 High/Med markets:** Show whatever is available. If 0, show "No high-confidence markets in this theme."
- **LLM produces more than 8 bullets:** Truncate to 8 in the UI.
- **Stale summaries:** Summaries are regenerated on each fetch+extract run. The `generated_at` timestamp shows freshness.

---

## 6. Dashboard UI Specification

### 6.1 Page Structure

The dashboard is a two-page SPA (same as v1). Page 1 (Market Explorer) is rebuilt. Page 2 (BTC Deep Dive) is carried over unchanged.

### 6.2 Market Explorer Layout

From top to bottom:

#### Header Bar (unchanged from v1)
- "Allium Prediction Markets Intelligence" + PROTOTYPE badge
- Navigation tabs: Market Explorer | BTC Deep Dive
- BTC price display (right-aligned)

#### Theme Selector

Large, prominent buttons — not small filter pills. These are the primary navigation for the page. Styled as cards or large toggle buttons with the theme name and market count.

```
┌───────────┐ ┌───────┐ ┌──────────┐ ┌────────┐ ┌────────────┐
│ Energies  │ │ Rates │ │ Equities │ │ Crypto │ │Geopolitics │
│    (14)   │ │  (15) │ │   (258)  │ │  (203) │ │    (60)    │
└───────────┘ └───────┘ └──────────┘ └────────┘ └────────────┘
┌─────────────┐ ┌─────────┐ ┌──────────┐
│ Commodities │ │  Macro  │ │ Politics │    [All]    [+ Other]
│     (42)    │ │   (7)   │ │   (35)   │
└─────────────┘ └─────────┘ └──────────┘
```

**Behavior:**
- **Default state:** "All" is selected. Shows all markets EXCEPT "Other". No context paragraph shown. No sub-filters shown.
- **Click a theme:** That theme highlights (blue border + background). Context paragraph appears. Sub-filters appear. Table filters to that theme's markets. Other theme buttons dim but remain clickable.
- **Multi-select:** Clicking additional themes adds them (OR logic). Context shows for the most recently clicked theme. Sub-filters show for the most recently clicked theme.
- **Click active theme again:** Deselects it. If no themes selected, reverts to "All" state.
- **"+ Other" toggle:** Small, unobtrusive toggle button. Off by default. When on, "Other" markets appear in the table alongside themed markets. When "All" is active and "+ Other" is on, literally everything shows.

#### Theme Context Panel

Appears between theme selector and sub-filters when a theme is selected. Subtle card with muted background.

```
┌──────────────────────────────────────────────────────────────┐
│ Iran-Israel war closed Strait of Hormuz on March 4,          │
│ stranding 20% of global oil supply. Brent at $120/bbl,       │
│ largest supply disruption in IEA history. Qatar LNG force     │
│ majeure. Venezuela US strikes threatening additional supply.  │
│                                                Updated Mar 27 │
└──────────────────────────────────────────────────────────────┘
```

**Behavior:**
- Shows the `context` string from `theme_contexts.json` for the selected theme.
- "Updated" timestamp from `theme_contexts.json`'s `updated_at` field.
- When "All" is selected, this panel is hidden.
- When multiple themes are selected, show context for the most recently clicked one, with the theme name as a label above the context text.

#### Sub-Filters

Small filter pills specific to the selected theme. Only appear when a single theme (or the most recent theme in multi-select) is active.

```
[All] [Oil] [Natural Gas] [Nuclear] [Shipping] [Renewables]
```

**Behavior:**
- Default: "All" selected (no sub-filter active, all markets in theme shown).
- Click a sub-filter: Filters table to markets matching that sub-filter's keywords in the question text.
- Sub-filters are OR within the group (clicking "Oil" then "Shipping" shows markets matching either).
- Sub-filter keywords are defined in `theme_contexts.json` → `sub_keywords`.
- When theme selection changes, sub-filters reset to "All".

#### Standard Filters Row

Below sub-filters. These apply on top of theme/sub-filter selection (AND logic).

```
Prob: [<20%] [20-40%] [40-60%] [60-80%] [>80%]
Vol:  [>$1M] [$100K+] [$10K+] [<$10K]
Quality: [High] [Med] [Thin] [Inactive]
Venue: [All] [Polymarket] [Kalshi]
[Search markets...........................] 243 markets
```

**Quality tiers** (same as v1 latest):
- High: ≥100 traders AND ≥$100K volume (green dot)
- Med: ≥50 traders AND ≥$50K volume (yellow dot)
- Thin: ≥10 traders AND ≥$10K volume (orange dot)
- Inactive: everything else (gray dot)

**Note on quality for v2:** Since we're not pre-enriching with Allium trader data, the quality column will initially only use volume from native APIs. Trader counts will be null until a market is clicked and enriched. The quality column should gracefully handle missing trader data:
- If trader count is available: use the full quality formula above.
- If trader count is null: use volume-only tiers ($100K+ = Med or above indicator, $10K+ = Thin, below = Inactive). Display without the trader count parenthetical.

#### Market Table

| Column | Content | Sort | Notes |
|--------|---------|------|-------|
| Market | Question text + theme tags | Alpha | Tags shown as small colored pills below question |
| Prob / Price | Probability or consensus price | Numeric | For binary markets: "65.0%" with bar. For price markets: "$67.0K -2.7%" with color-coded delta |
| 7d Volume | Volume in last 7 days | Numeric | "$6.1M", "$245K", etc. |
| Quality | Quality tier dot + label | By tier | See quality tiers above. Show "(142)" trader count only if available. |
| Resolves | Days to resolution | Numeric | "Today" (pink), "2d" (yellow), "14d" (gray) |
| Venue | Venue badge | Alpha | Colored badge: purple=Polymarket, blue=Kalshi |
| Insights | Insight badges | — | Contested (40-60%), High Signal (high quality + volume), Resolving Soon (≤2d + volume) |

**Row click behavior:** Opens a side panel (or expands the row) showing:
- Full question text and description
- Link to market on native venue (Polymarket slug or Kalshi URL)
- "Loading enrichment..." → fires Allium query for this market's `event_ticker`
- Once loaded: unique trader count, top 5 wallets (Polymarket only), whale concentration

The enrichment query is a single Allium SQL call scoped to the specific event. This is the on-demand pattern — no pre-enrichment needed.

**Table defaults:**
- Sorted by 7d volume descending
- 200 rows shown (with "showing 200 of N" indicator)
- No pagination — scroll. If >200 filtered markets, show first 200 with a note.

### 6.3 BTC Deep Dive (Page 2)

Unchanged from v1. Loads `btc_deep_dive.json`. Contains:
- Hero chart (spot price + distribution bands)
- Band detail table with date tabs
- Source comparison chart (Polymarket vs Kalshi)
- Top wallets panel

### 6.4 Styling

Dark theme carried over from v1:
- Background: `#0a0e17`
- Cards: `#111827` with `#1c2333` borders
- Text: `#e1e4e8` (primary), `#9ca3af` (secondary), `#6b7280` (muted)
- Accent: `#3b82f6` (blue), `#4ade80` (green), `#f87171` (red), `#facc15` (yellow)

Theme selector buttons should be visually distinct from filter pills — larger, with theme-specific subtle color tints or icons. Active theme has a clear highlighted state (e.g., blue border + slight background tint).

Theme context panel: `#0d1117` background, `#1c2333` border, slightly inset. Text in `#9ca3af`. "Updated" timestamp in `#4b5563`.

---

## 7. On-Demand Enrichment

When a user clicks a market row, the dashboard fires a request to get Allium enrichment data for that specific market.

### 7.1 Implementation Options

**Option A — Python backend:** Run a lightweight Flask/FastAPI server that accepts an event_ticker, queries Allium, and returns JSON. The dashboard fetches from this local endpoint.

**Option B — Pre-generated enrichment endpoint:** Have a script that, given an event_ticker, queries Allium and writes a small JSON file to a known path. The dashboard fetches that file.

**Option C — Inline in fetch_markets.py:** Not recommended — defeats the purpose of on-demand.

**Recommended for prototype: Option A** with a minimal Flask server, or even simpler: a static file approach where clicking a market opens a new browser tab with the enrichment data pre-rendered.

For the initial build, we can stub the enrichment panel with "Enrichment data requires Allium connection — click to query" and implement the actual Allium call as a fast-follow.

### 7.2 Allium Query for Single Market Enrichment

```sql
-- Unique traders and volume breakdown
SELECT COALESCE(maker, taker) AS wallet,
       SUM(usd_amount) AS total_volume,
       COUNT(*) AS trade_count,
       token_outcome
FROM crosschain.predictions.trades
WHERE project = '{venue}'
  AND event_ticker = '{event_ticker}'
  AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
  AND COALESCE(maker, taker) IS NOT NULL
GROUP BY wallet, token_outcome
ORDER BY total_volume DESC
```

From this single query, compute:
- Unique trader count (count distinct wallets)
- Top 10 wallets by volume
- Whale concentration (top 5 wallets as % of total volume)
- Yes vs No positioning breakdown

**Note:** This only works for Polymarket (has wallet addresses). For Kalshi markets, the enrichment panel should show "Wallet data not available — Kalshi is a centralized exchange" instead of empty data.

---

## 8. API Reference

### Polymarket Gamma API
- **Base:** `https://gamma-api.polymarket.com`
- **Auth:** None (public)
- **Rate limits:** Undocumented, but sustained 1 req/sec works fine
- **Key endpoints:**
  - `GET /events?active=true&closed=false&limit=100&offset=N` — list events with nested markets
  - `GET /markets?closed=false&limit=100&offset=N` — list markets directly
- **Gotcha:** `outcomePrices` is a JSON string, not an array. Parse with `json.loads()` or string manipulation.
- **Gotcha:** `volume`, `volume24hr`, etc. are double-counted (2x real volume). Always divide by 2.

### Kalshi REST API
- **Base:** `https://api.elections.kalshi.com/trade-api/v2`
- **Auth:** None for market data (public). Auth required for trading.
- **Rate limits:** Undocumented, sustained 2 req/sec works
- **Key endpoints:**
  - `GET /markets?status=open&limit=1000&cursor=X` — list markets
  - `GET /markets?event_ticker=X` — list markets for specific event
  - `GET /markets/trades?ticker=X&limit=100` — trade history
- **Gotcha:** `volume_fp` and `volume_24h_fp` are in **contracts** (number of shares), not dollars. Multiply by price to approximate USD volume.
- **Gotcha:** Cursor-based pagination. Stop when `cursor` is null or empty in response.

### Allium Explorer API
- **Base:** `POST https://api.allium.so/api/v1/explorer/queries`
- **Auth:** `X-API-Key` header required
- **Rate limits:** Queries >2000 rows frequently 503/524. Keep limits at 2000.
- **Flow:** POST to create query → get `query_id` → POST `/{query_id}/run` → get results
- **Gotcha:** `TIMESTAMP_TZ` columns must be cast to `::timestamp_ntz` to avoid Parquet export errors
- **Gotcha:** Kalshi `token_price` is NULL in `crosschain.predictions.markets`. Get prices from latest trades.
- **Key tables:**
  - `crosschain.predictions.trades` — trade-level data with wallet addresses (Polymarket maker/taker, Kalshi NULL)
  - `crosschain.predictions.markets` — market metadata (but limited coverage)
  - `polygon.wallet_features.wallet_360` — wallet enrichment data

---

## 9. Known Constraints and Gotchas

1. **Jupiter data is broken.** As of ~March 11 2026, all Jupiter trades in `solana.predictions.trades` have NULL tickers, user_addresses, prices, and actions. Jupiter also absent from `crosschain.predictions.markets`. Not usable until Allium fixes ingestion. Leave Jupiter out of v2 for now.

2. **Polymarket volume double-counting.** Every volume figure from Polymarket's Gamma API must be divided by 2. This is well-documented (Paradigm, Dec 2025) and verified by our comparison tests. Allium's figures are already correct (one-sided).

3. **Kalshi volume units.** `volume_fp` = contracts, not dollars. We verified this exactly: Allium's `SUM(num_shares)` == Kalshi's `volume_fp` for every tested market. Multiply by average price for USD approximation.

4. **Kalshi 7-day volume not available.** The Kalshi API provides `volume_fp` (all-time) and `volume_24h_fp` (24h) but no 7-day field. Options: (a) estimate as `volume_24h * 7` (rough), (b) use all-time volume for ranking, (c) leave null. Recommendation: use `volume_24h * 7` as estimate, note it in the column header tooltip.

5. **Quality column without Allium.** Since we're not pre-enriching, trader counts are unavailable on page load. Quality must initially use volume-only thresholds. Trader count appears only after click-to-enrich.

6. **Theme classification depends on Claude agents.** The `extract_themes.py` step requires running through Claude Code. It cannot be run as a standalone Python script without LLM access. If the pipeline needs to be fully automated, this step would need an Anthropic API key and SDK integration.

7. **Large market counts.** With ~64K markets pre-filter, the fetch step will take 5-10 minutes (paginating through both APIs). After filtering, ~5-10K markets. After theme extraction, the JSON file could be 5-10MB. The dashboard should handle this gracefully — show a loading state, render incrementally, don't freeze on large datasets.

8. **Cross-venue market matching.** The same event may exist on both Polymarket and Kalshi with different wording. V2 does NOT attempt to match these — they appear as separate rows. Cross-venue comparison is deferred to a future version. The venue column and filter let users see both and compare manually.

9. **Stale theme overrides.** A market tagged "Energies" 3 months ago because of Iran might no longer be energy-relevant if the conflict resolves. For the prototype, we accept this staleness. A production system would periodically re-classify markets whose context has shifted (detectable by comparing the current theme_contexts.json against the one that was active when the market was classified).

10. **Consensus price approximation.** The Kalshi volume-to-USD conversion (contracts × last_price) is approximate because price varies over the life of the market. For ranking purposes this is fine. For display, the consensus price computed from band probabilities is exact.
