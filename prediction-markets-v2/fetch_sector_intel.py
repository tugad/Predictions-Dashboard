"""
Prediction Markets V2 — Sector Intelligence Fetcher
=====================================================
Pre-fetches Allium wallet analytics per sector using ILIKE patterns.

For each sector:
  - Top wallets across all sector markets
  - Cross-sector flow (what else do top wallets trade)
  - Whale concentration + positioning
  - Unusual activity detection

Stores results in explorer_data.json under sector_intel key.
Run after extract_themes.py. Takes ~12 minutes for all sectors.
"""

import json
import requests
import time

API_KEY = "47lh6ohmjsMWFl8znEqjFXCKP53Qsg0-e9O47Djmk6NCnMcvj52TZPp6h5ljwmUnhkrkRXuV4hp9gCsd9xJmgg"
BASE_URL = "https://api.allium.so/api/v1/explorer/queries"
HEADERS = {"X-API-Key": API_KEY, "Content-Type": "application/json"}

# ILIKE patterns per sector — targeted for fast Allium queries
SECTOR_WHERE = {
    "Energies": "question ILIKE '%crude oil%' OR question ILIKE '%iran%ceasefire%' OR question ILIKE '%iran%enter%' OR question ILIKE '%gas price%' OR question ILIKE '%hormuz%' OR question ILIKE '%iran%regime%' OR question ILIKE '%oil%hit%' OR question ILIKE '%iran%invade%'",
    "Rates": "question ILIKE '%fed%interest%' OR question ILIKE '%federal reserve%' OR question ILIKE '%fomc%' OR question ILIKE '%rate cut%' OR question ILIKE '%inflation%increase%' OR question ILIKE '%powell%chair%' OR question ILIKE '%rate hike%' OR question ILIKE '%cpi%'",
    "Equities": "question ILIKE '%market cap%largest%' OR question ILIKE '%nvidia%' OR question ILIKE '%tesla%deliver%' OR question ILIKE '%ipo%' OR question ILIKE '%deepseek%' OR question ILIKE '%S&P%500%'",
    "Crypto": "question ILIKE '%bitcoin%' OR question ILIKE '%btc%' OR question ILIKE '%ethereum%' OR question ILIKE '%solana%' OR question ILIKE '%xrp%' OR question ILIKE '%crypto%capital%'",
    "Geopolitics": "question ILIKE '%iran%military%' OR question ILIKE '%israel%military%' OR question ILIKE '%russia%capture%' OR question ILIKE '%ukraine%ceasefire%' OR question ILIKE '%nato%' OR question ILIKE '%trump%china%visit%' OR question ILIKE '%ceasefire%march%'",
    "Commodities": "question ILIKE '%crude oil%hit%' OR question ILIKE '%gold%gc%hit%' OR question ILIKE '%gold%settle%' OR question ILIKE '%silver%' OR question ILIKE '%comex%'",
    "Macro": "question ILIKE '%unemployment%reach%' OR question ILIKE '%gdp%growth%' OR question ILIKE '%recession%' OR question ILIKE '%inflation%annual%'",
    "Elections": "question ILIKE '%2028%nominee%' OR question ILIKE '%governor%election%' OR question ILIKE '%shutdown%' OR question ILIKE '%trump%out%president%'",
}


def allium_query(sql, title="query", limit=2000):
    for attempt in range(3):
        try:
            resp = requests.post(BASE_URL, headers=HEADERS, json={
                "config": {"sql": sql, "limit": limit}, "title": title,
            }, timeout=30)
            query_id = resp.json()["query_id"]
            result = requests.post(
                f"{BASE_URL}/{query_id}/run",
                headers=HEADERS, json={}, timeout=300
            )
            if result.status_code != 200:
                print(f"  [{title}] HTTP {result.status_code}, retry {attempt+1}")
                time.sleep(5 * (attempt + 1))
                continue
            data = result.json().get("data", [])
            print(f"  [{title}] {len(data)} rows", flush=True)
            return data
        except Exception as e:
            print(f"  [{title}] Error: {e}")
            time.sleep(5)
    print(f"  [{title}] FAILED")
    return []


def fetch_sector_intel(sector, where):
    """Fetch wallet analytics for a sector. Returns dict."""
    print(f"\n=== {sector} ===")

    # Query 1: Top wallets
    wallets_raw = allium_query(f"""
        SELECT COALESCE(maker, taker) AS wallet,
               SUM(usd_amount) AS total_volume,
               COUNT(*) AS trade_count,
               SUM(CASE WHEN token_outcome = 'Yes' THEN usd_amount ELSE 0 END) AS yes_vol,
               SUM(CASE WHEN token_outcome = 'No' THEN usd_amount ELSE 0 END) AS no_vol
        FROM crosschain.predictions.trades
        WHERE project = 'polymarket'
          AND ({where})
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
          AND COALESCE(maker, taker) IS NOT NULL
        GROUP BY wallet
        ORDER BY total_volume DESC
    """, f"{sector} wallets", limit=100)

    if not wallets_raw:
        return None

    total_vol = sum(float(r.get("total_volume") or 0) for r in wallets_raw)
    top5_vol = sum(float(r.get("total_volume") or 0) for r in wallets_raw[:5])
    top10_vol = sum(float(r.get("total_volume") or 0) for r in wallets_raw[:10])
    total_yes = sum(float(r.get("yes_vol") or 0) for r in wallets_raw)
    total_no = sum(float(r.get("no_vol") or 0) for r in wallets_raw)
    pos_total = total_yes + total_no

    top_wallets = []
    for r in wallets_raw[:10]:
        addr = r.get("wallet", "")
        vol = float(r.get("total_volume") or 0)
        yes_v = float(r.get("yes_vol") or 0)
        no_v = float(r.get("no_vol") or 0)
        top_wallets.append({
            "address": addr,
            "short": addr[:6] + ".." + addr[-4:] if len(addr) > 10 else addr,
            "volume": round(vol, 2),
            "pct": round(vol / total_vol * 100, 1) if total_vol > 0 else 0,
            "trades": int(r.get("trade_count") or 0),
            "side": "YES" if yes_v > no_v else "NO",
        })

    # Query 2: Cross-sector activity for top 3 wallets
    cross_sector = {}
    top3 = [w["address"] for w in top_wallets[:3]]
    if top3:
        addr_list = ", ".join(f"'{a}'" for a in top3)
        cross = allium_query(f"""
            SELECT COALESCE(maker, taker) AS wallet,
                   question,
                   SUM(usd_amount) AS volume
            FROM crosschain.predictions.trades
            WHERE COALESCE(maker, taker) IN ({addr_list})
              AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
              AND NOT ({where})
            GROUP BY wallet, question
            ORDER BY wallet, volume DESC
        """, f"{sector} cross-sector", limit=200)

        for r in cross:
            w = r.get("wallet", "")
            short = w[:6] + ".." + w[-4:] if len(w) > 10 else w
            if short not in cross_sector:
                cross_sector[short] = []
            if len(cross_sector[short]) < 5:
                cross_sector[short].append({
                    "question": r.get("question", ""),
                    "volume": round(float(r.get("volume") or 0), 2),
                })

    # Query 3: Unusual activity (24h vs 7d wallet count)
    unusual_raw = allium_query(f"""
        WITH recent AS (
            SELECT question,
                   COUNT(DISTINCT COALESCE(maker, taker)) AS wallets_24h,
                   SUM(usd_amount) AS vol_24h
            FROM crosschain.predictions.trades
            WHERE project = 'polymarket' AND ({where})
              AND trade_timestamp::timestamp_ntz >= DATEADD(hour, -24, CURRENT_TIMESTAMP)
            GROUP BY question
        ),
        baseline AS (
            SELECT question,
                   COUNT(DISTINCT COALESCE(maker, taker)) / 7.0 AS avg_daily_wallets
            FROM crosschain.predictions.trades
            WHERE project = 'polymarket' AND ({where})
              AND trade_timestamp::timestamp_ntz >= DATEADD(day, -7, CURRENT_TIMESTAMP)
            GROUP BY question
        )
        SELECT r.question, r.wallets_24h, r.vol_24h, b.avg_daily_wallets,
               r.wallets_24h / NULLIF(b.avg_daily_wallets, 0) AS wallet_ratio
        FROM recent r JOIN baseline b ON r.question = b.question
        WHERE r.wallets_24h >= 5
        ORDER BY wallet_ratio DESC
    """, f"{sector} unusual", limit=20)

    unusual = []
    for r in unusual_raw:
        ratio = float(r.get("wallet_ratio") or 0)
        if ratio > 1.5:
            unusual.append({
                "question": r.get("question", ""),
                "wallets_24h": int(r.get("wallets_24h") or 0),
                "avg_daily": round(float(r.get("avg_daily_wallets") or 0), 1),
                "ratio": round(ratio, 1),
                "vol_24h": round(float(r.get("vol_24h") or 0), 2),
            })

    return {
        "total_volume_30d": round(total_vol, 2),
        "unique_wallets": len(wallets_raw),
        "whale_concentration": {
            "top5_pct": round(top5_vol / total_vol * 100, 1) if total_vol > 0 else 0,
            "top10_pct": round(top10_vol / total_vol * 100, 1) if total_vol > 0 else 0,
        },
        "positioning": {
            "yes_pct": round(total_yes / pos_total * 100, 1) if pos_total > 0 else 50,
            "no_pct": round(total_no / pos_total * 100, 1) if pos_total > 0 else 50,
        },
        "top_wallets": top_wallets,
        "cross_sector": cross_sector,
        "unusual_activity": unusual[:5],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Sector Intelligence Fetcher")
    print("=" * 60)

    start = time.time()
    sector_intel = {}

    for sector, where in SECTOR_WHERE.items():
        result = fetch_sector_intel(sector, where)
        if result:
            sector_intel[sector] = result
            print(f"  -> {result['unique_wallets']} wallets, "
                  f"top5={result['whale_concentration']['top5_pct']}%, "
                  f"{len(result['unusual_activity'])} unusual")

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"Done: {len(sector_intel)} sectors, {elapsed:.0f}s")

    # Merge into explorer_data.json
    explorer = json.load(open("explorer_data.json"))
    explorer["sector_intel"] = sector_intel
    with open("explorer_data.json", "w") as f:
        json.dump(explorer, f)
    print(f"Updated explorer_data.json")


if __name__ == "__main__":
    main()
