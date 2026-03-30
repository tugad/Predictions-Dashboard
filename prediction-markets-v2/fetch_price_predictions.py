"""
Prediction Markets V2 — Price Predictions Data Fetcher
========================================================
Fetches spot prices from Hyperliquid and builds probability distributions
from prediction market data for BTC, Crude Oil, and Gold.

Outputs price_predictions.json.
"""

import json
import os
import re
import requests
import time
from collections import defaultdict

HYPERLIQUID_API = "https://api.hyperliquid.xyz/info"

ASSETS = {
    "BTC": {
        "hyperliquid_coin": "BTC",
        "label": "Bitcoin",
        "unit": "$",
        "band_step": 2000,
        "band_min": 50000,
        "band_max": 90000,
        "question_patterns": [
            r"bitcoin", r"\bbtc\b",
        ],
        "price_extractors": {
            "hit_high": r"hit \(HIGH\) \$(\d[\d,]*)",
            "hit_low": r"hit \(LOW\) \$(\d[\d,]*)",
            "above": r"above \$(\d[\d,]*)",
            "below": r"below \$(\d[\d,]*)",
            "settle_range": r"settle at \$(\d[\d,]*)-\$(\d[\d,]*)",
            "settle_over": r"settle over \$(\d[\d,]*)",
        },
        "date_patterns": [
            (r"on March 28", "2026-03-28"),
            (r"on March 29", "2026-03-29"),
            (r"on March 30", "2026-03-30"),
            (r"on March 31|March 31|Mar 31|end of March|in March", "2026-03-31"),
            (r"on April 1", "2026-04-01"),
            (r"on April 2", "2026-04-02"),
            (r"on April 3", "2026-04-03"),
            (r"on April 4", "2026-04-04"),
            (r"end of April|April 30|Apr 30", "2026-04-30"),
            (r"by May 1|May 1", "2026-05-01"),
            (r"end of June|June 30|Jun 30|by June 30", "2026-06-30"),
            (r"by October 1|October 1", "2026-10-01"),
            (r"end of December|December 31|Dec 31|before 2027|by Jan 1, 2027|by Dec", "2026-12-31"),
        ],
    },
    "OIL": {
        "hyperliquid_coin": "xyz:CL",
        "label": "Crude Oil (WTI)",
        "unit": "$",
        "band_step": 5,
        "band_min": 60,
        "band_max": 220,
        "question_patterns": [
            r"crude oil", r"\bwti\b", r"oil \(cl\)",
        ],
        "price_extractors": {
            "hit_high": r"hit \(HIGH\) \$(\d[\d,]*)",
            "hit_low": r"hit \(LOW\) \$(\d[\d,]*)",
            "settle_range": r"settle at \$(\d[\d,]*)-\$(\d[\d,]*)",
            "settle_over": r"settle over \$(\d[\d,]*)",
            "above": r"above[^\$]*\$(\d[\d,]*)",
            "below": r"below[^\$]*\$(\d[\d,]*)",
        },
        "date_patterns": [
            (r"on Mar 30", "2026-03-30"),
            (r"end of March|in March|on Mar 31|Mar 31", "2026-03-31"),
            (r"in April|end of April|on Apr 30|Apr 30", "2026-04-30"),
            (r"end of June|in June|on Jun 30|Jun 30", "2026-06-30"),
            (r"end of September|in September", "2026-09-30"),
            (r"end of December|by Dec|before 2027|in 2026|Dec 31", "2026-12-31"),
        ],
    },
    "GOLD": {
        "hyperliquid_coin": "xyz:GOLD",
        "label": "Gold",
        "unit": "$",
        "band_step": 200,
        "band_min": 3000,
        "band_max": 10000,
        "question_patterns": [
            r"gold", r"\bgc\b", r"\bxau\b",
        ],
        "price_extractors": {
            "hit_high": r"hit \(HIGH\) \$(\d[\d,]*)",
            "hit_low": r"hit \(LOW\) \$(\d[\d,]*)",
            "settle_range": r"settle at[^\$]*\$(\d[\d,]*)-\$(\d[\d,]*)",
            "settle_over": r"settle over \$(\d[\d,]*)",
            "settle_under": r"settle at <\$(\d[\d,]*)",
            "above": r"above[^\$]*\$(\d[\d,]*)",
            "below": r"below[^\$]*\$(\d[\d,]*)",
        },
        "date_patterns": [
            (r"on Mar 30", "2026-03-30"),
            (r"end of March|in March|March 2026|on Mar 31|final.*March", "2026-03-31"),
            (r"end of April|in April|on Apr 30", "2026-04-30"),
            (r"end of June|in June|June 2026|on Jun 30", "2026-06-30"),
            (r"end of September|in September", "2026-09-30"),
            (r"end of December|by Dec|before 2027|Dec 2026|Dec 31", "2026-12-31"),
        ],
    },
}


def fetch_hyperliquid_candles(coin, days=30):
    """Fetch daily candles from Hyperliquid."""
    end = int(time.time() * 1000)
    start = end - (days * 24 * 60 * 60 * 1000)
    try:
        resp = requests.post(HYPERLIQUID_API, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": "1d", "startTime": start, "endTime": end},
        }, timeout=15)
        data = resp.json()
        if isinstance(data, list):
            return [
                {
                    "date": time.strftime("%Y-%m-%d", time.gmtime(c["t"] / 1000)),
                    "open": float(c["o"]),
                    "high": float(c["h"]),
                    "low": float(c["l"]),
                    "close": float(c["c"]),
                    "volume": float(c["v"]),
                }
                for c in data
            ]
    except Exception as e:
        print(f"  Error fetching {coin}: {e}")
    return []


def parse_price(s):
    """Parse a price string like '100' or '3,600' to float."""
    return float(s.replace(",", ""))


def extract_date(question, patterns):
    """Extract resolution date from question text."""
    ql = question.lower()
    for pattern, date in patterns:
        if re.search(pattern, question, re.IGNORECASE):
            return date
    return None


def build_scenario_table(markets, asset_config):
    """Build key level probabilities across dates from hit/above/below markets."""
    # Group by date and level
    levels = defaultdict(lambda: defaultdict(list))

    for m in markets:
        q = m["question"]
        ql = q.lower()
        prob = m.get("price") or 0
        vol = m.get("volume_7d") or 0
        venue = m.get("venue", "")

        date = extract_date(q, asset_config["date_patterns"])
        if not date:
            continue

        for mtype, pattern in asset_config["price_extractors"].items():
            match = re.search(pattern, q, re.IGNORECASE)
            if not match:
                continue

            if mtype in ("hit_high", "above"):
                price = parse_price(match.group(1))
                levels[price][date].append({
                    "type": "above", "prob": prob, "vol": vol, "venue": venue, "question": q,
                })
            elif mtype in ("hit_low", "below"):
                price = parse_price(match.group(1))
                levels[price][date].append({
                    "type": "below", "prob": prob, "vol": vol, "venue": venue, "question": q,
                })
            elif mtype == "settle_over":
                price = parse_price(match.group(1))
                levels[price][date].append({
                    "type": "above", "prob": prob, "vol": vol, "venue": venue, "question": q,
                })
            elif mtype == "settle_under":
                price = parse_price(match.group(1))
                levels[price][date].append({
                    "type": "below", "prob": prob, "vol": vol, "venue": venue, "question": q,
                })
            elif mtype == "settle_range":
                lo = parse_price(match.group(1))
                hi = parse_price(match.group(2))
                mid = (lo + hi) / 2
                levels[mid][date].append({
                    "type": "range", "lo": lo, "hi": hi, "prob": prob, "vol": vol,
                    "venue": venue, "question": q,
                })
            break

    # Build scenario rows sorted by price level
    dates = sorted(set(d for lvl in levels.values() for d in lvl.keys()))
    rows = []
    for price in sorted(levels.keys()):
        row = {"level": price, "dates": {}}
        for date in dates:
            entries = levels[price].get(date, [])
            if entries:
                # Volume-weighted average probability
                total_vol = sum(e["vol"] for e in entries)
                if total_vol > 0:
                    avg_prob = sum(e["prob"] * e["vol"] for e in entries) / total_vol
                else:
                    avg_prob = sum(e["prob"] for e in entries) / len(entries)
                row["dates"][date] = {
                    "prob": round(avg_prob, 4),
                    "vol": round(total_vol, 2),
                    "sources": len(entries),
                    "type": entries[0]["type"],
                }
        rows.append(row)

    return {"dates": dates, "levels": rows}


def compute_fan(asset_markets, config, scenarios):
    """Compute probability fan (percentile intervals) from CDF-like market data."""
    date_patterns = config["date_patterns"]
    price_extractors = config["price_extractors"]

    # Collect CDF points: (price, P(above)) per date
    cdf_by_date = {}
    for m in asset_markets:
        q = m["question"]
        prob = m.get("price") or 0
        if prob <= 0:
            continue

        # Extract date
        date = extract_date(q, date_patterns)
        if not date:
            continue

        ql = q.lower()
        for mtype, pattern in price_extractors.items():
            match = re.search(pattern, q, re.IGNORECASE)
            if not match:
                continue
            if mtype in ("hit_high", "above", "settle_over"):
                price = parse_price(match.group(1))
                cdf_by_date.setdefault(date, []).append((price, prob))
            elif mtype in ("hit_low", "below", "settle_under"):
                price = parse_price(match.group(1))
                cdf_by_date.setdefault(date, []).append((price, 1 - prob))
            break

    def percentile_from_cdf(pts, pctl):
        pts = sorted(pts, key=lambda x: x[0])
        target = 1 - pctl
        for i in range(len(pts) - 1):
            p1, prob1 = pts[i]
            p2, prob2 = pts[i + 1]
            if (prob1 >= target >= prob2) or (prob2 >= target >= prob1):
                if abs(prob1 - prob2) < 0.001:
                    return (p1 + p2) / 2
                t = (target - prob1) / (prob2 - prob1)
                return p1 + t * (p2 - p1)
        if target >= pts[0][1]:
            return pts[0][0]
        return pts[-1][0]

    fan = {}
    for date, points in sorted(cdf_by_date.items()):
        if len(points) < 4:
            continue
        f = {
            "p10": round(percentile_from_cdf(points, 0.10), 2),
            "p25": round(percentile_from_cdf(points, 0.25), 2),
            "p50": round(percentile_from_cdf(points, 0.50), 2),
            "p75": round(percentile_from_cdf(points, 0.75), 2),
            "p90": round(percentile_from_cdf(points, 0.90), 2),
        }
        if f["p10"] < f["p50"] < f["p90"]:
            fan[date] = f

    return fan


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Price Predictions Fetcher")
    print("=" * 60)

    explorer = json.load(open("explorer_data.json"))
    all_markets = explorer["markets"]
    output = {}

    for asset_key, config in ASSETS.items():
        print(f"\n=== {config['label']} ===")

        # Fetch spot prices from Hyperliquid
        candles = fetch_hyperliquid_candles(config["hyperliquid_coin"])
        current_price = candles[-1]["close"] if candles else 0
        print(f"  Spot: ${current_price:,.2f} ({len(candles)} daily candles)")

        # Find prediction markets for this asset
        asset_markets = []
        for m in all_markets:
            ql = m["question"].lower()
            if any(re.search(p, ql) for p in config["question_patterns"]):
                if any(k in ql for k in ["hit", "above", "below", "price", "settle", "reach"]):
                    asset_markets.append(m)

        print(f"  Prediction markets: {len(asset_markets)}")

        # Build scenario table
        scenarios = build_scenario_table(asset_markets, config)
        print(f"  Scenario levels: {len(scenarios['levels'])}")
        print(f"  Dates: {scenarios['dates']}")

        # Build source comparison (Polymarket vs Kalshi per level)
        poly_markets = [m for m in asset_markets if m["venue"] == "polymarket"]
        kalshi_markets = [m for m in asset_markets if m["venue"] == "kalshi"]
        print(f"  Polymarket: {len(poly_markets)}, Kalshi: {len(kalshi_markets)}")

        # Compute fan percentiles from CDF data
        fan = compute_fan(asset_markets, config, scenarios)
        print(f"  Fan dates: {list(fan.keys())}")

        output[asset_key] = {
            "label": config["label"],
            "unit": config["unit"],
            "current_price": current_price,
            "spot_prices": candles,
            "band_step": config["band_step"],
            "band_min": config["band_min"],
            "band_max": config["band_max"],
            "scenarios": scenarios,
            "fan": fan,
            "market_count": len(asset_markets),
            "markets": [
                {
                    "question": m["question"],
                    "price": m.get("price") or 0,
                    "volume_7d": m.get("volume_7d") or 0,
                    "venue": m["venue"],
                    "end_date": m.get("end_date", ""),
                }
                for m in sorted(asset_markets, key=lambda x: x.get("volume_7d") or 0, reverse=True)
            ],
        }

    # Generate tenor data from fan + scenarios
    for asset_key, config in ASSETS.items():
        d = output.get(asset_key, {})
        fan = d.get("fan", {})
        spot = d.get("current_price", 0)
        sc = d.get("scenarios", {})
        mkts = d.get("markets", [])

        tenors = []
        for date in sorted(fan.keys()):
            f = fan[date]
            # Count markets for this date
            date_markets = [m for m in mkts if date[:7] in (m.get("end_date") or "")[:7]]
            total_vol = sum(m.get("volume_7d", 0) for m in date_markets)

            tenors.append({
                "date": date,
                "market_count": len(date_markets),
                "total_volume": round(total_vol, 2),
                "median": f["p50"],
                "p10": f["p10"], "p25": f["p25"], "p75": f["p75"], "p90": f["p90"],
                "median_vs_spot": round((f["p50"] - spot) / spot * 100, 1) if spot > 0 else 0,
                "key_levels": [],
                "narrative": "",
            })

        d["tenors"] = tenors
        print(f"  {asset_key} tenors: {len(tenors)}")

    # Generate tenor narratives via Anthropic API if available
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and any(len(output.get(k, {}).get("tenors", [])) > 0 for k in ASSETS):
        print("\n=== GENERATING TENOR NARRATIVES ===")
        for asset_key, config in ASSETS.items():
            d = output.get(asset_key, {})
            tenors = d.get("tenors", [])
            if not tenors:
                continue

            spot = d.get("current_price", 0)
            tenor_block = ""
            for t in tenors:
                tenor_block += f"\n--- {t['date']} ({t['market_count']} markets, ${t['total_volume']:,.0f} volume) ---\n"
                tenor_block += f"Spot: ${spot:,.2f}\n"
                tenor_block += f"Median: ${t['median']:,.0f} ({t['median_vs_spot']:+.1f}% from spot)\n"
                tenor_block += f"Range: 10th=${t['p10']:,.0f}  25th=${t['p25']:,.0f}  75th=${t['p75']:,.0f}  90th=${t['p90']:,.0f}\n"

            prompt = f"""You are writing concise market commentary for institutional portfolio managers about {config['label']} price predictions.

For each tenor below, write 2-3 sentences summarizing what prediction markets are saying. Be specific about probabilities and price levels.

Current spot: ${spot:,.2f}

{tenor_block}

Output a JSON object mapping each date to its narrative string.
Output ONLY the JSON object."""

            try:
                resp = requests.post("https://api.anthropic.com/v1/messages", headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }, json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 2048,
                    "messages": [{"role": "user", "content": prompt}],
                }, timeout=60)

                if resp.status_code == 200:
                    text = resp.json()["content"][0]["text"]
                    # Strip code fences
                    text = re.sub(r'^```(?:json)?\s*\n?', '', text.strip())
                    text = re.sub(r'\n?```\s*$', '', text.strip())
                    narratives = json.loads(text)
                    for t in tenors:
                        if t["date"] in narratives:
                            t["narrative"] = narratives[t["date"]]
                    print(f"  {asset_key}: {sum(1 for t in tenors if t['narrative'])} narratives")
                else:
                    print(f"  {asset_key}: API error {resp.status_code}")
            except Exception as e:
                print(f"  {asset_key}: error {e}")

    output["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    with open("price_predictions.json", "w") as f:
        json.dump(output, f, indent=2)

    size_kb = len(json.dumps(output)) / 1024
    print(f"\nSaved price_predictions.json ({size_kb:.0f} KB)")
    print("Done.")


if __name__ == "__main__":
    main()
