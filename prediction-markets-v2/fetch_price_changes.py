"""
Prediction Markets V2 — Price Change Fetcher
==============================================
Fetches 24h and 7d price history from Polymarket CLOB API
for top markets by volume. Enriches markets_raw.json with
price_24h_ago and price_7d_ago fields.

Run after fetch_markets.py, before extract_themes.py.
"""

import json
import requests
import time
import sys

CLOB_BASE = "https://clob.polymarket.com"
MIN_VOLUME = 50000  # Only fetch for High/Med markets


def fetch_price_history(token_id, interval="1d", fidelity=60):
    """Fetch price history from Polymarket CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB_BASE}/prices-history",
            params={"market": token_id, "interval": interval, "fidelity": fidelity},
            timeout=10,
        )
        data = resp.json()
        return data.get("history", [])
    except Exception:
        return []


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Price Change Fetcher")
    print("=" * 60)

    data = json.load(open("markets_raw.json"))
    markets = data["markets"]

    # Find Polymarket markets with token IDs and sufficient volume
    poly_targets = [
        m for m in markets
        if m.get("venue") == "polymarket"
        and m.get("clob_token_id")
        and (m.get("volume_7d") or 0) >= MIN_VOLUME
    ]

    # Sort by volume descending, cap at 300
    poly_targets.sort(key=lambda m: m.get("volume_7d") or 0, reverse=True)
    poly_targets = poly_targets[:300]

    print(f"\n  Polymarket targets: {len(poly_targets)} markets (vol >= ${MIN_VOLUME:,})")

    # Also handle Kalshi — previous_price already captured in fetch_markets.py
    kalshi_with_prev = sum(
        1 for m in markets
        if m.get("venue") == "kalshi"
        and m.get("previous_price", 0) > 0
        and (m.get("volume_7d") or 0) >= MIN_VOLUME
    )
    print(f"  Kalshi with previous_price: {kalshi_with_prev}")

    # Compute Kalshi 24h changes immediately
    for m in markets:
        if m.get("venue") == "kalshi" and m.get("previous_price", 0) > 0:
            price = m.get("price") or 0
            prev = m.get("previous_price") or 0
            if price > 0 and prev > 0:
                m["change_24h"] = round((price - prev) * 100, 2)
            else:
                m["change_24h"] = None
            m["change_7d"] = None  # Not available from Kalshi API

    # Fetch Polymarket price changes
    print(f"\n  Fetching CLOB price history for {len(poly_targets)} markets...")
    print(f"  Estimated time: ~{len(poly_targets) * 2 / 10 * 0.3 + len(poly_targets) * 0.2:.0f}s\n")

    # Build token_id → market index mapping
    tid_to_indices = {}
    for i, m in enumerate(markets):
        tid = m.get("clob_token_id")
        if tid:
            tid_to_indices.setdefault(tid, []).append(i)

    fetched = 0
    errors = 0
    start_time = time.time()

    # Process in batches of 10
    batch_size = 10
    for batch_start in range(0, len(poly_targets), batch_size):
        batch = poly_targets[batch_start:batch_start + batch_size]

        for m in batch:
            tid = m["clob_token_id"]
            current_price = m.get("price") or 0

            # Fetch 24h
            h24 = fetch_price_history(tid, "1d", 60)
            # Fetch 7d
            h7d = fetch_price_history(tid, "1w", 360)

            price_24h_ago = h24[0]["p"] if len(h24) >= 2 else None
            price_7d_ago = h7d[0]["p"] if len(h7d) >= 2 else None

            change_24h = round((current_price - price_24h_ago) * 100, 2) if price_24h_ago is not None and current_price > 0 else None
            change_7d = round((current_price - price_7d_ago) * 100, 2) if price_7d_ago is not None and current_price > 0 else None

            # Apply to all markets with this token_id
            for idx in tid_to_indices.get(tid, []):
                markets[idx]["price_24h_ago"] = price_24h_ago
                markets[idx]["price_7d_ago"] = price_7d_ago
                markets[idx]["change_24h"] = change_24h
                markets[idx]["change_7d"] = change_7d

            fetched += 1

        # Progress
        elapsed = time.time() - start_time
        if (batch_start // batch_size) % 5 == 0:
            print(f"  [{fetched}/{len(poly_targets)}] {elapsed:.0f}s elapsed")

        time.sleep(0.2)  # Small delay between batches

    elapsed = time.time() - start_time
    print(f"\n  Done: {fetched} fetched, {errors} errors, {elapsed:.0f}s total")

    # Summary of changes
    with_24h = sum(1 for m in markets if m.get("change_24h") is not None)
    with_7d = sum(1 for m in markets if m.get("change_7d") is not None)
    print(f"  Markets with 24h change: {with_24h}")
    print(f"  Markets with 7d change: {with_7d}")

    # Show biggest movers
    movers_24h = [(m, m["change_24h"]) for m in markets if m.get("change_24h") is not None]
    movers_24h.sort(key=lambda x: abs(x[1]), reverse=True)
    print(f"\n  Top 10 24h movers:")
    for m, c in movers_24h[:10]:
        print(f"    {c:>+6.1f}pp  {m['question'][:60]}")

    # Save
    with open("markets_raw.json", "w") as f:
        json.dump(data, f)

    size_mb = len(json.dumps(data)) / 1024 / 1024
    print(f"\n  Updated markets_raw.json ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
