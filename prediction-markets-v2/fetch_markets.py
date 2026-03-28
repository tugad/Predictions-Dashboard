"""
Prediction Markets V2 — Market Fetcher
========================================
Fetches all active markets from Polymarket (Gamma API) and Kalshi (REST API).
Normalizes into a common schema, filters noise, computes consensus prices.
Outputs markets_raw.json.

See PROJECT.md sections 4.1–4.4 for full specification.
"""

import json
import re
import requests
import time
from collections import defaultdict

POLY_BASE = "https://gamma-api.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ============================================================
# POLYMARKET
# ============================================================

def fetch_polymarket():
    """Fetch all active markets from Polymarket Gamma API."""
    print("\n=== POLYMARKET (Gamma API) ===")
    markets = []
    seen = set()
    offset = 0
    page = 0

    while True:
        try:
            resp = requests.get(
                f"{POLY_BASE}/events",
                params={"active": "true", "closed": "false", "limit": 100, "offset": offset},
                timeout=30,
            )
            events = resp.json()
        except Exception as e:
            print(f"  Error at offset {offset}: {e}")
            break

        if not events:
            break

        for evt in events:
            event_tags = [t.get("label", "") for t in evt.get("tags", [])]
            for mkt in evt.get("markets", []):
                q = mkt.get("question", "")
                if not q:
                    continue

                # Deduplicate
                if q in seen:
                    continue
                seen.add(q)

                # Filter micro markets
                ql = q.lower()
                if "5 min" in ql or "15 min" in ql or "up or down" in ql:
                    continue

                # Filter expired
                end_date = (mkt.get("endDate") or "")[:10]
                if end_date and end_date < time.strftime("%Y-%m-%d"):
                    continue

                # Filter dead markets
                liquidity = float(mkt.get("liquidity") or 0)
                if liquidity < 100:
                    continue

                # Parse price
                try:
                    prices_str = mkt.get("outcomePrices") or "[]"
                    price = float(prices_str.strip("[]").split(",")[0].strip('" ') or 0)
                except (ValueError, IndexError):
                    price = 0

                # Volumes — divide by 2 (Polymarket double-counts)
                vol_24h = float(mkt.get("volume24hr") or 0) / 2
                vol_7d = float(mkt.get("volume1wk") or 0) / 2
                vol_30d = float(mkt.get("volume1mo") or 0) / 2

                markets.append({
                    "venue": "polymarket",
                    "question": q,
                    "description": mkt.get("description") or "",
                    "price": round(price, 4),
                    "volume_24h": round(vol_24h, 2),
                    "volume_7d": round(vol_7d, 2),
                    "volume_30d": round(vol_30d, 2),
                    "liquidity": round(liquidity, 2),
                    "end_date": end_date,
                    "native_tags": event_tags,
                    "slug": mkt.get("slug") or "",
                    "clob_token_id": (json.loads(mkt.get("clobTokenIds") or "[]") or [None])[0],
                    "condition_id": mkt.get("conditionId") or "",
                    "event_ticker": None,
                    "market_ticker": None,
                    "is_price_market": False,
                    "consensus_price": None,
                    "peak_band": None,
                    "peak_prob": None,
                    "spot_price": None,
                    "themes": None,
                    "sub_tags": None,
                })

        offset += 100
        page += 1
        if len(events) < 100:
            break
        if page % 10 == 0:
            print(f"  Fetched {page * 100} events, {len(markets)} markets so far...")

    print(f"  Total: {len(markets)} Polymarket markets")
    return markets


# ============================================================
# KALSHI
# ============================================================

def fetch_kalshi():
    """Fetch all open markets from Kalshi REST API."""
    print("\n=== KALSHI (REST API) ===")
    raw_markets = []
    cursor = None
    page = 0

    while True:
        try:
            url = f"{KALSHI_BASE}/markets?status=open&limit=1000"
            if cursor:
                url += f"&cursor={cursor}"
            resp = requests.get(url, timeout=30)
            data = resp.json()
        except Exception as e:
            print(f"  Error at page {page}: {e}")
            break

        batch = data.get("markets", [])
        if not batch:
            break

        raw_markets.extend(batch)
        cursor = data.get("cursor")
        page += 1

        if page % 5 == 0:
            print(f"  Fetched {len(raw_markets)} raw markets...")

        if not cursor:
            break

    print(f"  Raw markets: {len(raw_markets)}")

    # Group by event_ticker to detect price-range markets
    by_event = defaultdict(list)
    for m in raw_markets:
        evt = m.get("event_ticker", "")
        by_event[evt].append(m)

    markets = []
    seen_events_as_price = set()

    for evt, evt_markets in by_event.items():
        # Check if this is a price-range event (multiple markets with -B or -T suffixes)
        band_markets = [m for m in evt_markets if "-B" in m.get("ticker", "") or "-T" in m.get("ticker", "")]
        is_price_event = len(band_markets) >= 3

        if is_price_event:
            # Compute consensus price from bands
            consensus = compute_consensus_price(band_markets)
            if consensus:
                # Use the first market's metadata for the parent entry
                rep = evt_markets[0]
                end_date = (rep.get("close_time") or "")[:10]

                if end_date and end_date < time.strftime("%Y-%m-%d"):
                    continue

                # Aggregate volume across all bands
                total_vol_contracts = sum(float(m.get("volume_fp") or 0) for m in evt_markets)
                total_vol_24h_contracts = sum(float(m.get("volume_24h_fp") or 0) for m in evt_markets)
                total_liquidity = sum(float(m.get("liquidity_dollars") or 0) for m in evt_markets)

                # Approximate USD volume using consensus price for avg price
                avg_price = consensus["avg_price"] if consensus["avg_price"] > 0 else 0.5
                vol_24h_usd = total_vol_24h_contracts * avg_price
                vol_7d_usd = vol_24h_usd * 7  # Estimate

                # Detect asset for spot price
                spot = detect_spot_price(evt)

                title = consensus.get("title", rep.get("title", evt))
                markets.append({
                    "venue": "kalshi",
                    "question": title,
                    "description": rep.get("rules_primary") or "",
                    "price": None,
                    "volume_24h": round(vol_24h_usd, 2),
                    "volume_7d": round(vol_7d_usd, 2),
                    "volume_30d": None,
                    "liquidity": round(total_liquidity, 2),
                    "end_date": end_date,
                    "native_tags": [],
                    "slug": None,
                    "event_ticker": evt,
                    "market_ticker": None,
                    "is_price_market": True,
                    "consensus_price": consensus["consensus_price"],
                    "peak_band": consensus["peak_band"],
                    "peak_prob": consensus["peak_prob"],
                    "spot_price": spot,
                    "themes": None,
                    "sub_tags": None,
                })
                seen_events_as_price.add(evt)
                continue

        # Non-price-range markets: emit each individually
        for m in evt_markets:
            ticker = m.get("ticker", "")
            title = m.get("title") or ""
            end_date = (m.get("close_time") or "")[:10]

            if end_date and end_date < time.strftime("%Y-%m-%d"):
                continue

            vol_contracts = float(m.get("volume_fp") or 0)
            vol_24h_contracts = float(m.get("volume_24h_fp") or 0)
            last_price = float(m.get("last_price_dollars") or 0)
            liquidity = float(m.get("liquidity_dollars") or 0)

            if vol_contracts == 0 and liquidity == 0:
                continue

            price_for_conversion = last_price if last_price > 0 else 0.5
            vol_24h_usd = vol_24h_contracts * price_for_conversion
            vol_7d_usd = vol_24h_usd * 7

            markets.append({
                "venue": "kalshi",
                "question": title,
                "description": m.get("rules_primary") or "",
                "price": round(last_price, 4) if last_price > 0 else None,
                "volume_24h": round(vol_24h_usd, 2),
                "volume_7d": round(vol_7d_usd, 2),
                "volume_30d": None,
                "liquidity": round(liquidity, 2),
                "end_date": end_date,
                "native_tags": [],
                "slug": None,
                "clob_token_id": None,
                "condition_id": "",
                "previous_price": float(m.get("previous_price_dollars") or 0),
                "event_ticker": evt,
                "market_ticker": ticker,
                "is_price_market": False,
                "consensus_price": None,
                "peak_band": None,
                "peak_prob": None,
                "spot_price": None,
                "themes": None,
                "sub_tags": None,
            })

    print(f"  Total: {len(markets)} Kalshi markets ({len(seen_events_as_price)} price-range events collapsed)")
    return markets


def compute_consensus_price(band_markets):
    """Compute probability-weighted average price from a set of band markets."""
    bands = []
    for m in band_markets:
        ticker = m.get("ticker", "")
        price = float(m.get("last_price_dollars") or 0)
        if price <= 0:
            continue

        # Parse strike from ticker: -B65750 or -T79199.99
        strike = None
        if "-B" in ticker:
            try:
                strike = float(ticker.split("-B")[-1])
            except ValueError:
                pass
        elif "-T" in ticker:
            try:
                strike = float(ticker.split("-T")[-1])
            except ValueError:
                pass

        if strike is not None:
            bands.append({"strike": strike, "prob": price})

    if len(bands) < 2:
        return None

    total_prob = sum(b["prob"] for b in bands)
    if total_prob <= 0:
        return None

    consensus = sum(b["strike"] * b["prob"] for b in bands) / total_prob
    peak = max(bands, key=lambda b: b["prob"])
    avg_price = total_prob / len(bands)

    # Build a readable title from the event
    title = band_markets[0].get("title") or band_markets[0].get("ticker", "")

    return {
        "consensus_price": round(consensus, 2),
        "peak_band": round(peak["strike"], 2),
        "peak_prob": round(peak["prob"], 4),
        "avg_price": avg_price,
        "num_bands": len(bands),
        "title": title,
    }


# Spot prices — updated manually or via web search
SPOT_PRICES = {
    "BTC": 68878, "ETH": 2071, "SOL": 87,
    "BNB": 620, "SP500": 5700, "NDX": 19800,
}

SPOT_PATTERNS = {
    "BTC": [r"bitcoin", r"\bbtc\b"],
    "ETH": [r"ethereum", r"\beth\b"],
    "SOL": [r"solana", r"\bsol\b"],
    "BNB": [r"\bbnb\b"],
    "SP500": [r"s&p", r"sp500", r"spy\b"],
    "NDX": [r"nasdaq", r"ndx\b", r"qqq\b"],
}


def detect_spot_price(event_ticker_or_question):
    """Detect which asset a price market is for and return its spot price."""
    text = event_ticker_or_question.lower()
    for asset, patterns in SPOT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text):
                return SPOT_PRICES.get(asset)
    return None


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("Prediction Markets V2 — Market Fetcher")
    print("=" * 60)

    poly_markets = fetch_polymarket()
    kalshi_markets = fetch_kalshi()

    all_markets = poly_markets + kalshi_markets

    # Summary
    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Polymarket: {len(poly_markets)}")
    print(f"  Kalshi:     {len(kalshi_markets)}")
    print(f"  Total:      {len(all_markets)}")

    price_markets = [m for m in all_markets if m["is_price_market"]]
    print(f"  Price-range events: {len(price_markets)}")

    # Write output
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spot_prices": SPOT_PRICES,
        "markets": all_markets,
    }

    with open("markets_raw.json", "w") as f:
        json.dump(output, f, indent=2)

    size_mb = len(json.dumps(output)) / 1024 / 1024
    print(f"\n  Saved markets_raw.json ({size_mb:.1f} MB)")
    print("  Done.")


if __name__ == "__main__":
    main()
