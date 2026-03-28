"""
Prediction Markets V2 — BTC Deep Dive Data Fetcher
====================================================
Carried over from v1. Fetches BTC prediction market distributions
and wallet analysis from Allium.

Outputs btc_deep_dive.json.
"""

import json
import re
import requests
import time
from collections import defaultdict

API_KEY = "47lh6ohmjsMWFl8znEqjFXCKP53Qsg0-e9O47Djmk6NCnMcvj52TZPp6h5ljwmUnhkrkRXuV4hp9gCsd9xJmgg"
BASE_URL = "https://api.allium.so/api/v1/explorer/queries"
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}


def run_query(sql, title="query", limit=500, retries=4):
    for attempt in range(retries + 1):
        try:
            resp = requests.post(BASE_URL, headers=HEADERS, json={
                "config": {"sql": sql, "limit": limit},
                "title": title,
            }, timeout=30)
            query_id = resp.json()["query_id"]
            result = requests.post(f"{BASE_URL}/{query_id}/run", headers=HEADERS, json={}, timeout=180)
            if result.status_code != 200:
                wait = 5 * (attempt + 1)
                print(f"  [{title}] HTTP {result.status_code}, retry {attempt+1}, waiting {wait}s")
                time.sleep(wait)
                continue
            data = result.json().get("data", [])
            print(f"  [{title}] {len(data)} rows")
            return data
        except Exception as e:
            wait = 5 * (attempt + 1)
            print(f"  [{title}] Error: {e}, retry {attempt+1}, waiting {wait}s")
            time.sleep(wait)
    print(f"  [{title}] FAILED after {retries+1} attempts")
    return []


def fetch_btc_distributions():
    """Fetch BTC prediction market data and compute distributions."""
    print("\n=== BTC DISTRIBUTIONS ===")

    poly_markets = run_query("""
        SELECT event_ticker, market_name, question, token_price, token_outcome,
               end_date::timestamp_ntz AS end_date
        FROM crosschain.predictions.markets
        WHERE project = 'polymarket'
          AND (question ILIKE '%bitcoin%' OR question ILIKE '%btc%')
          AND end_date::timestamp_ntz >= CURRENT_DATE
          AND end_date::timestamp_ntz <= DATEADD(day, 7, CURRENT_DATE)
          AND market_status = 'active'
          AND (question ILIKE '%above%' OR question ILIKE '%between%'
               OR question ILIKE '%less than%' OR question ILIKE '%greater than%')
          AND token_outcome = 'Yes'
          AND question LIKE 'Will the price of Bitcoin%'
        ORDER BY end_date, question
    """, "Polymarket BTC daily")

    kalshi_trades = run_query("""
        WITH latest AS (
            SELECT market_ticker, event_ticker, yes_price, usd_amount,
                   ROW_NUMBER() OVER (PARTITION BY market_ticker ORDER BY trade_timestamp DESC) AS rn
            FROM crosschain.predictions.trades
            WHERE project = 'kalshi'
              AND event_ticker LIKE 'KXBTCD%'
              AND trade_timestamp::timestamp_ntz >= DATEADD(day, -7, CURRENT_TIMESTAMP)
        )
        SELECT market_ticker, event_ticker, yes_price, usd_amount
        FROM latest WHERE rn = 1
        ORDER BY event_ticker, market_ticker
    """, "Kalshi BTC daily latest")

    poly_volumes = run_query("""
        SELECT question, SUM(usd_amount) AS total_volume, COUNT(*) AS trade_count
        FROM crosschain.predictions.trades
        WHERE project = 'polymarket'
          AND (question ILIKE '%bitcoin%' OR question ILIKE '%btc%')
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -7, CURRENT_TIMESTAMP)
          AND question LIKE 'Will the price of Bitcoin%'
        GROUP BY question
    """, "Polymarket BTC volumes")

    vol_lookup = {v["question"]: v.get("total_volume", 0) or 0 for v in poly_volumes}

    bands = list(range(56000, 84001, 2000))

    def parse_strike(question):
        prices = re.findall(r'\$(\d[\d,]*)', question)
        if not prices:
            return None, None
        prices = [int(p.replace(',', '')) for p in prices]
        q = question.lower()
        if 'above' in q:
            return 'above', prices[0]
        elif 'between' in q:
            return 'range', (prices[0], prices[1])
        elif 'less than' in q:
            return 'below', prices[0]
        elif 'greater than' in q:
            return 'above', prices[0]
        return None, None

    def parse_date(question):
        m = re.search(r'(?:on|On) March (\d+)', question)
        if m:
            return f"2026-03-{int(m.group(1)):02d}"
        return None

    poly_above = defaultdict(list)
    poly_range = defaultdict(list)

    for m in poly_markets:
        q = m.get("question", "")
        price = m.get("token_price", 0) or 0
        date = parse_date(q)
        mtype, strike = parse_strike(q)
        vol = vol_lookup.get(q, 0)
        if not date or not mtype:
            continue
        if mtype == 'above':
            poly_above[date].append({"strike": strike, "prob": price, "vol": vol})
        elif mtype == 'range':
            poly_range[date].append({"lo": strike[0], "hi": strike[1], "prob": price, "vol": vol})
        elif mtype == 'below':
            poly_range[date].append({"lo": 0, "hi": strike, "prob": price, "vol": vol})

    kalshi_above = defaultdict(list)
    for t in kalshi_trades:
        ticker = t.get("market_ticker", "")
        evt = t.get("event_ticker", "")
        yes_price = t.get("yes_price", 0) or 0
        usd = t.get("usd_amount", 0) or 0
        dm = re.search(r'26MAR(\d{2})', evt)
        if not dm:
            continue
        date = f"2026-03-{int(dm.group(1)):02d}"
        if '-T' not in ticker:
            continue
        strike = float(ticker.split('-T')[-1])
        bucket = round(strike / 2000) * 2000
        kalshi_above[date].append({"strike": bucket, "prob": yes_price, "vol": usd})

    def cdf_to_pdf(cdf_dict):
        pdf = {}
        keys = sorted(cdf_dict.keys())
        for i, s in enumerate(keys):
            if i < len(keys) - 1:
                pdf[s] = max(0, cdf_dict[s] - cdf_dict[keys[i + 1]])
            else:
                pdf[s] = cdf_dict[s]
        return pdf

    distributions = {}
    all_dates = sorted(set(list(poly_above.keys()) + list(kalshi_above.keys())))

    for date in all_dates:
        p_cdf = {p["strike"]: p["prob"] for p in sorted(poly_above.get(date, []), key=lambda x: x["strike"])}
        p_vol = {p["strike"]: p["vol"] for p in poly_above.get(date, [])}
        p_pdf = cdf_to_pdf(p_cdf)

        r_pdf = {r["lo"]: r["prob"] for r in poly_range.get(date, [])}
        r_vol = {r["lo"]: r["vol"] for r in poly_range.get(date, [])}

        k_buckets = defaultdict(list)
        k_vol_b = defaultdict(float)
        for k in kalshi_above.get(date, []):
            k_buckets[k["strike"]].append(k["prob"])
            k_vol_b[k["strike"]] += k["vol"]
        k_cdf = {b: sum(ps) / len(ps) for b, ps in k_buckets.items()}
        k_vol = dict(k_vol_b)
        k_pdf = cdf_to_pdf(k_cdf)

        combined = []
        for band in bands:
            sources = []
            if band in p_pdf:
                sources.append({"prob": p_pdf[band], "vol": p_vol.get(band, 0), "src": "polymarket_above"})
            if band in r_pdf:
                sources.append({"prob": r_pdf[band], "vol": r_vol.get(band, 0), "src": "polymarket_range"})
            if band in k_pdf:
                sources.append({"prob": k_pdf[band], "vol": k_vol.get(band, 0), "src": "kalshi"})

            if not sources:
                combined.append({"price": band, "prob": 0, "volume": 0, "sources": {}})
                continue

            total_vol = sum(s["vol"] for s in sources)
            if total_vol > 0:
                wp = sum(s["prob"] * s["vol"] for s in sources) / total_vol
            else:
                wp = sum(s["prob"] for s in sources) / len(sources)

            combined.append({
                "price": band,
                "prob": round(wp, 4),
                "volume": round(total_vol, 2),
                "sources": {s["src"]: round(s["prob"], 4) for s in sources},
            })

        distributions[date] = combined

    return distributions


def fetch_btc_wallets():
    """Fetch top wallets trading BTC prediction markets."""
    print("\n=== WALLET ANALYSIS ===")

    top_wallets = run_query("""
        SELECT COALESCE(maker, taker) AS wallet,
               SUM(usd_amount) AS total_volume,
               COUNT(*) AS trade_count,
               MIN(trade_timestamp::timestamp_ntz) AS first_trade,
               MAX(trade_timestamp::timestamp_ntz) AS last_trade
        FROM crosschain.predictions.trades
        WHERE project = 'polymarket'
          AND (question ILIKE '%bitcoin%' OR question ILIKE '%btc%')
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
          AND COALESCE(maker, taker) IS NOT NULL
        GROUP BY wallet
        ORDER BY total_volume DESC
    """, "Top BTC wallets", limit=30)

    if not top_wallets:
        return []

    addresses = [w["wallet"] for w in top_wallets if w.get("wallet")]
    if not addresses:
        return []

    addr_list = ", ".join(f"'{a}'" for a in addresses[:10])
    other_markets = run_query(f"""
        SELECT COALESCE(maker, taker) AS wallet,
               question,
               category,
               SUM(usd_amount) AS total_volume,
               COUNT(*) AS trade_count
        FROM crosschain.predictions.trades
        WHERE COALESCE(maker, taker) IN ({addr_list})
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
          AND question NOT ILIKE '%bitcoin%'
          AND question NOT ILIKE '%btc%'
        GROUP BY wallet, question, category
        ORDER BY wallet, total_volume DESC
    """, "Other markets for top wallets", limit=500)

    other_by_wallet = defaultdict(list)
    for m in other_markets:
        w = m.get("wallet", "")
        other_by_wallet[w].append({
            "question": m.get("question", ""),
            "category": m.get("category", ""),
            "volume": round(float(m.get("total_volume") or 0), 2),
            "trades": int(m.get("trade_count") or 0),
        })

    wallet_data = []
    for w in top_wallets:
        addr = w.get("wallet", "")
        wallet_data.append({
            "address": addr,
            "short_address": addr[:6] + "..." + addr[-4:] if len(addr) > 10 else addr,
            "btc_volume": round(float(w.get("total_volume") or 0), 2),
            "btc_trades": int(w.get("trade_count") or 0),
            "first_trade": str(w.get("first_trade", ""))[:10],
            "last_trade": str(w.get("last_trade", ""))[:10],
            "other_markets": other_by_wallet.get(addr, [])[:10],
        })

    return wallet_data


def main():
    print("=" * 60)
    print("Prediction Markets V2 — BTC Deep Dive Fetcher")
    print("=" * 60)

    distributions = fetch_btc_distributions()

    spot_prices = [
        {"date": "2026-03-14", "price": 70965},
        {"date": "2026-03-15", "price": 71217},
        {"date": "2026-03-16", "price": 72682},
        {"date": "2026-03-17", "price": 74858},
        {"date": "2026-03-18", "price": 73926},
        {"date": "2026-03-19", "price": 71256},
        {"date": "2026-03-20", "price": 69871},
        {"date": "2026-03-21", "price": 70553},
        {"date": "2026-03-22", "price": 68734},
        {"date": "2026-03-23", "price": 67849},
        {"date": "2026-03-24", "price": 70893},
        {"date": "2026-03-25", "price": 70525},
        {"date": "2026-03-26", "price": 68494},
        {"date": "2026-03-27", "price": 68878},
    ]

    wallets = fetch_btc_wallets()

    with open("btc_deep_dive.json", "w") as f:
        json.dump({
            "spot_prices": spot_prices,
            "current_price": 68878,
            "distributions": distributions,
            "wallets": wallets,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, f, indent=2)

    print(f"\nSaved btc_deep_dive.json ({len(distributions)} dates, {len(wallets)} wallets)")
    print("Done.")


if __name__ == "__main__":
    main()
