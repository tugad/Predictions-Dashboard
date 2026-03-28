"""
Prediction Markets V2 — Server
================================
Serves static files AND handles /api/enrich for live Allium enrichment.
No Flask dependency — uses Python's built-in http.server.

Usage:
    python3 serve.py
    → http://127.0.0.1:8091/dashboard.html
"""

import http.server
import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
import requests

PORT = 8091
ALLIUM_API_KEY = "47lh6ohmjsMWFl8znEqjFXCKP53Qsg0-e9O47Djmk6NCnMcvj52TZPp6h5ljwmUnhkrkRXuV4hp9gCsd9xJmgg"
ALLIUM_BASE = "https://api.allium.so/api/v1/explorer/queries"
ALLIUM_HEADERS = {"X-API-Key": ALLIUM_API_KEY, "Content-Type": "application/json"}


def allium_query(sql, title="query", limit=200):
    """Run an Allium SQL query. Returns list of dicts."""
    for attempt in range(3):
        try:
            resp = requests.post(ALLIUM_BASE, headers=ALLIUM_HEADERS, json={
                "config": {"sql": sql, "limit": limit}, "title": title,
            }, timeout=30)
            query_id = resp.json()["query_id"]
            result = requests.post(
                f"{ALLIUM_BASE}/{query_id}/run",
                headers=ALLIUM_HEADERS, json={}, timeout=180
            )
            if result.status_code != 200:
                time.sleep(3)
                continue
            return result.json().get("data", [])
        except Exception as e:
            time.sleep(3)
    return []


def enrich_market(question, venue):
    """Run all enrichment queries for a single market. Returns dict."""
    # Escape single quotes in question for SQL
    q_safe = question.replace("'", "''")
    is_polymarket = venue == "polymarket"

    results = {}
    errors = []

    def run_query(key, sql, title, limit=200):
        try:
            data = allium_query(sql, title, limit)
            results[key] = data
        except Exception as e:
            errors.append(f"{key}: {e}")
            results[key] = []

    # Fire all queries in parallel threads
    threads = []

    # 1. Hourly price history (7 days)
    threads.append(threading.Thread(target=run_query, args=(
        "price_history",
        f"""
        SELECT DATE_TRUNC('hour', trade_timestamp::timestamp_ntz) AS hour,
               AVG(yes_price) AS avg_price,
               SUM(usd_amount) AS volume,
               COUNT(*) AS trades,
               COUNT(DISTINCT COALESCE(maker, taker)) AS unique_wallets
        FROM crosschain.predictions.trades
        WHERE project = '{venue}'
          AND question = '{q_safe}'
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -7, CURRENT_TIMESTAMP)
        GROUP BY hour
        ORDER BY hour
        """,
        "price_history",
    )))

    # 2. Yes/No breakdown + overall stats
    threads.append(threading.Thread(target=run_query, args=(
        "positioning",
        f"""
        SELECT token_outcome,
               SUM(usd_amount) AS volume,
               COUNT(*) AS trades,
               COUNT(DISTINCT COALESCE(maker, taker)) AS unique_wallets
        FROM crosschain.predictions.trades
        WHERE project = '{venue}'
          AND question = '{q_safe}'
          AND trade_timestamp::timestamp_ntz >= DATEADD(day, -7, CURRENT_TIMESTAMP)
        GROUP BY token_outcome
        """,
        "positioning",
    )))

    # 3. Top wallets + whale concentration (Polymarket only)
    if is_polymarket:
        threads.append(threading.Thread(target=run_query, args=(
            "wallets",
            f"""
            SELECT COALESCE(maker, taker) AS wallet,
                   SUM(usd_amount) AS total_volume,
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN token_outcome = 'Yes' THEN usd_amount ELSE 0 END) AS yes_volume,
                   SUM(CASE WHEN token_outcome = 'No' THEN usd_amount ELSE 0 END) AS no_volume,
                   MIN(trade_timestamp::timestamp_ntz) AS first_trade,
                   MAX(trade_timestamp::timestamp_ntz) AS last_trade
            FROM crosschain.predictions.trades
            WHERE project = 'polymarket'
              AND question = '{q_safe}'
              AND trade_timestamp::timestamp_ntz >= DATEADD(day, -30, CURRENT_TIMESTAMP)
              AND COALESCE(maker, taker) IS NOT NULL
            GROUP BY wallet
            ORDER BY total_volume DESC
            """,
            "wallets",
        )))

        # 4. Recent large trades
        threads.append(threading.Thread(target=run_query, args=(
            "large_trades",
            f"""
            SELECT trade_timestamp::timestamp_ntz AS ts,
                   token_outcome, yes_price, usd_amount,
                   COALESCE(maker, taker) AS wallet
            FROM crosschain.predictions.trades
            WHERE project = 'polymarket'
              AND question = '{q_safe}'
              AND trade_timestamp::timestamp_ntz >= DATEADD(day, -3, CURRENT_TIMESTAMP)
              AND usd_amount >= 500
            ORDER BY usd_amount DESC
            """,
            "large_trades",
        )))

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    # Process results
    enrichment = {"question": question, "venue": venue, "errors": errors}

    # Price history
    ph = results.get("price_history", [])
    enrichment["price_history"] = [
        {
            "hour": r.get("hour", ""),
            "price": round(float(r.get("avg_price") or 0), 4),
            "volume": round(float(r.get("volume") or 0), 2),
            "trades": int(r.get("trades") or 0),
            "wallets": int(r.get("unique_wallets") or 0),
        }
        for r in ph
    ]

    # Positioning
    pos = results.get("positioning", [])
    yes_vol = 0
    no_vol = 0
    total_trades = 0
    total_wallets = 0
    for r in pos:
        outcome = (r.get("token_outcome") or "").lower()
        vol = float(r.get("volume") or 0)
        trades = int(r.get("trades") or 0)
        wallets = int(r.get("unique_wallets") or 0)
        total_trades += trades
        total_wallets += wallets
        if outcome == "yes":
            yes_vol = vol
        elif outcome == "no":
            no_vol = vol

    total_vol = yes_vol + no_vol
    enrichment["positioning"] = {
        "yes_volume": round(yes_vol, 2),
        "no_volume": round(no_vol, 2),
        "yes_pct": round(yes_vol / total_vol * 100, 1) if total_vol > 0 else 0,
        "no_pct": round(no_vol / total_vol * 100, 1) if total_vol > 0 else 0,
        "total_volume_7d": round(total_vol, 2),
        "total_trades_7d": total_trades,
        "unique_wallets_7d": total_wallets,
    }

    # Wallets
    wallet_data = results.get("wallets", [])
    total_wallet_vol = sum(float(r.get("total_volume") or 0) for r in wallet_data)

    top_wallets = []
    for r in wallet_data[:10]:
        addr = r.get("wallet", "")
        vol = float(r.get("total_volume") or 0)
        yes_v = float(r.get("yes_volume") or 0)
        no_v = float(r.get("no_volume") or 0)
        top_wallets.append({
            "address": addr,
            "short": addr[:6] + ".." + addr[-4:] if len(addr) > 10 else addr,
            "volume": round(vol, 2),
            "pct": round(vol / total_wallet_vol * 100, 1) if total_wallet_vol > 0 else 0,
            "trades": int(r.get("trade_count") or 0),
            "side": "YES" if yes_v > no_v else "NO",
            "first_trade": str(r.get("first_trade", ""))[:10],
            "last_trade": str(r.get("last_trade", ""))[:10],
        })

    top5_vol = sum(w["volume"] for w in top_wallets[:5])
    enrichment["wallets"] = {
        "total_unique": len(wallet_data),
        "top5_concentration": round(top5_vol / total_wallet_vol * 100, 1) if total_wallet_vol > 0 else 0,
        "top_wallets": top_wallets,
        "smart_money_signal": top_wallets[0]["side"] if top_wallets else None,
        "top5_same_side": len(set(w["side"] for w in top_wallets[:5])) == 1 if len(top_wallets) >= 5 else False,
    }

    # Large trades
    lt = results.get("large_trades", [])
    enrichment["large_trades"] = [
        {
            "time": str(r.get("ts", ""))[:16],
            "side": r.get("token_outcome", "?"),
            "amount": round(float(r.get("usd_amount") or 0), 2),
            "wallet": (r.get("wallet") or "")[:6] + ".." + (r.get("wallet") or "")[-4:] if len(r.get("wallet") or "") > 10 else r.get("wallet", ""),
        }
        for r in lt[:15]
    ]

    return enrichment


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/enrich":
            self.handle_enrich(parsed)
        else:
            super().do_GET()

    def handle_enrich(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        question = params.get("question", [""])[0]
        venue = params.get("venue", ["polymarket"])[0]

        if not question:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "question parameter required"}).encode())
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        result = enrich_market(question, venue)
        self.wfile.write(json.dumps(result).encode())

    def log_message(self, format, *args):
        if "/api/enrich" in str(args[0]):
            print(f"  [enrich] {args[0]}")


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with socketserver.TCPServer(("127.0.0.1", PORT), Handler) as httpd:
        print(f"Prediction Markets V2 — Server")
        print(f"http://127.0.0.1:{PORT}/dashboard.html")
        print(f"Enrichment API: http://127.0.0.1:{PORT}/api/enrich?question=...&venue=...")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
