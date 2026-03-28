"""
Prediction Markets V2 — Price Change Fetcher
==============================================
Fetches 24h and 7d price history from Polymarket CLOB API
for ALL Polymarket markets. Uses concurrent threads for speed.
Enriches markets_raw.json with change_24h and change_7d fields.

Run after fetch_markets.py, before extract_themes.py.
"""

import json
import requests
import time
import concurrent.futures

CLOB_BASE = "https://clob.polymarket.com"
MAX_WORKERS = 20


def fetch_both_intervals(token_id):
    """Fetch 24h and 7d price history for a single token. Returns (tid, price_24h_ago, price_7d_ago)."""
    price_24h_ago = None
    price_7d_ago = None
    try:
        r1 = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": "1d", "fidelity": 60},
            timeout=10,
        )
        h1 = r1.json().get("history", [])
        if len(h1) >= 2:
            price_24h_ago = h1[0]["p"]
    except Exception:
        pass

    try:
        r7 = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": "1w", "fidelity": 360},
            timeout=10,
        )
        h7 = r7.json().get("history", [])
        if len(h7) >= 2:
            price_7d_ago = h7[0]["p"]
    except Exception:
        pass

    return token_id, price_24h_ago, price_7d_ago


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Price Change Fetcher")
    print("=" * 60)

    data = json.load(open("markets_raw.json"))
    markets = data["markets"]

    # Find ALL Polymarket markets with token IDs
    poly_targets = [
        m for m in markets
        if m.get("venue") == "polymarket"
        and m.get("clob_token_id")
    ]
    print(f"\n  Polymarket markets to fetch: {len(poly_targets)}")

    # Compute Kalshi 24h changes from previous_price (instant, no API call)
    kalshi_count = 0
    for m in markets:
        if m.get("venue") == "kalshi" and m.get("previous_price", 0) > 0:
            price = m.get("price") or 0
            prev = m.get("previous_price") or 0
            if price > 0 and prev > 0:
                m["change_24h"] = round((price - prev) * 100, 2)
                kalshi_count += 1
            else:
                m["change_24h"] = None
            m["change_7d"] = None

    print(f"  Kalshi 24h changes computed: {kalshi_count}")

    # Build token_id → market indices mapping
    tid_to_indices = {}
    for i, m in enumerate(markets):
        tid = m.get("clob_token_id")
        if tid:
            tid_to_indices.setdefault(tid, []).append(i)

    # Deduplicate token IDs (some markets share the same token)
    unique_tids = list(set(m["clob_token_id"] for m in poly_targets))
    print(f"  Unique token IDs: {len(unique_tids)}")
    print(f"  Estimated time: ~{len(unique_tids) * 2 / MAX_WORKERS / 10:.0f}-{len(unique_tids) * 2 / MAX_WORKERS / 5:.0f}s with {MAX_WORKERS} workers\n")

    start_time = time.time()
    results = {}
    fetched = 0
    errors = 0

    # Fetch all in parallel using thread pool
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_tid = {
            executor.submit(fetch_both_intervals, tid): tid
            for tid in unique_tids
        }

        for future in concurrent.futures.as_completed(future_to_tid):
            try:
                tid, p24, p7d = future.result()
                results[tid] = (p24, p7d)
                fetched += 1
            except Exception:
                errors += 1

            if fetched % 500 == 0:
                elapsed = time.time() - start_time
                print(f"  [{fetched}/{len(unique_tids)}] {elapsed:.0f}s elapsed")

    elapsed = time.time() - start_time
    print(f"\n  Fetched: {fetched}, errors: {errors}, time: {elapsed:.0f}s")

    # Apply results to markets
    applied = 0
    for tid, (p24, p7d) in results.items():
        for idx in tid_to_indices.get(tid, []):
            current_price = markets[idx].get("price") or 0
            markets[idx]["price_24h_ago"] = p24
            markets[idx]["price_7d_ago"] = p7d
            markets[idx]["change_24h"] = round((current_price - p24) * 100, 2) if p24 is not None and current_price > 0 else None
            markets[idx]["change_7d"] = round((current_price - p7d) * 100, 2) if p7d is not None and current_price > 0 else None
            applied += 1

    # Summary
    with_24h = sum(1 for m in markets if m.get("change_24h") is not None)
    with_7d = sum(1 for m in markets if m.get("change_7d") is not None)
    print(f"  Applied to {applied} market entries")
    print(f"  Markets with 24h change: {with_24h}")
    print(f"  Markets with 7d change: {with_7d}")

    # Top movers
    movers = [(m, m["change_24h"]) for m in markets if m.get("change_24h") is not None and (m.get("volume_7d") or 0) >= 50000]
    movers.sort(key=lambda x: abs(x[1]), reverse=True)
    print(f"\n  Top 10 24h movers (vol >= $50K):")
    for m, c in movers[:10]:
        print(f"    {c:>+6.1f}pp  ${m.get('volume_7d',0):>10,.0f}  {m['question'][:55]}")

    # Save
    with open("markets_raw.json", "w") as f:
        json.dump(data, f)

    size_mb = len(json.dumps(data)) / 1024 / 1024
    print(f"\n  Updated markets_raw.json ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
