"""
Prediction Markets V2 — API-based Theme Classification + Insights
==================================================================
Uses the Anthropic API (Claude Haiku) to:
  1. Classify new/untagged markets into themes
  2. Generate market intelligence summaries per theme
  3. Generate 24h/7d moves narratives per theme

Reads ANTHROPIC_API_KEY from environment variable.
Run after fetch_markets.py + fetch_price_changes.py + extract_themes.py.
"""

import json
import os
import re
import requests
import time
from collections import defaultdict

API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"

THEMES = ['Energies', 'Rates', 'Equities', 'Crypto', 'Geopolitics', 'Commodities', 'Macro', 'Elections', 'Sports']
MIN_VOLUME_MOVES = 50000


def call_claude(prompt, max_tokens=4096):
    """Call the Anthropic API. Returns response text with code fences stripped."""
    if not API_KEY:
        print("  WARNING: No ANTHROPIC_API_KEY set, skipping LLM call")
        return None
    try:
        resp = requests.post(API_URL, headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }, json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=120)
        if resp.status_code == 200:
            text = resp.json()["content"][0]["text"]
            # Strip markdown code fences if present
            text = re.sub(r'^```(?:json|python)?\s*\n?', '', text.strip())
            text = re.sub(r'\n?```\s*$', '', text.strip())
            return text
        else:
            print(f"  API error {resp.status_code}: {resp.json().get('error', {}).get('message', '')}")
            return None
    except Exception as e:
        print(f"  API exception: {e}")
        return None


def classify_new_markets(explorer_path, contexts_path, overrides_path):
    """Classify any markets not in theme_overrides.json."""
    print("\n=== CLASSIFYING NEW MARKETS ===")
    explorer = json.load(open(explorer_path))
    contexts = json.load(open(contexts_path))
    overrides = json.load(open(overrides_path))

    # Find markets that need classification:
    # 1. Not in overrides at all
    # 2. Tagged as "Other" by extract_themes.py (default for new markets)
    #    but only if they have decent volume (worth classifying)
    new_markets = []
    for m in explorer["markets"]:
        q = m["question"]
        if q not in overrides:
            new_markets.append(m)
        elif overrides[q] == ["Other"] and (m.get("volume_7d") or 0) >= 10000:
            # Previously defaulted to Other — reclassify if meaningful volume
            new_markets.append(m)

    if not new_markets:
        print("  No new markets to classify")
        return

    print(f"  {len(new_markets)} markets to classify")

    # Build context block
    ctx_block = ""
    for name, tdef in contexts["themes"].items():
        ctx_block += f"{name.upper()}: {tdef['context']}\n\n"

    # Process in batches of 100
    batch_size = 100
    for start in range(0, len(new_markets), batch_size):
        batch = new_markets[start:start + batch_size]
        market_list = ""
        for j, m in enumerate(batch):
            desc = (m.get("description") or "")[:150].replace("\n", " ")
            market_list += f"{j}: {m['question']}\n"
            if desc:
                market_list += f"   Context: {desc}\n"

        prompt = f"""You are tagging prediction markets for institutional portfolio managers.
Assign one or more themes from: Energies, Rates, Equities, Crypto, Geopolitics, Commodities, Macro, Elections, Sports, Other

If not relevant to any institutional theme AND not a sports market, tag as "Other".

=== THEME CONTEXTS ===
{ctx_block}
=== MARKETS ===
{market_list}

Output ONLY a Python dictionary mapping index to list of themes."""

        result = call_claude(prompt)
        if result:
            try:
                # Parse the dict from the response
                # Find the dict pattern in the response
                match = re.search(r'\{[^}]+\}', result, re.DOTALL)
                if match:
                    tags = eval(match.group(0))
                    for idx_str, themes in tags.items():
                        idx = int(idx_str)
                        if idx < len(batch):
                            overrides[batch[idx]["question"]] = themes
                    print(f"  Batch {start//batch_size + 1}: classified {len(tags)} markets")
            except Exception as e:
                print(f"  Batch {start//batch_size + 1}: parse error: {e}")

    # Save updated overrides
    with open(overrides_path, "w") as f:
        json.dump(overrides, f, indent=2)
    print(f"  Saved {len(overrides)} overrides")


def generate_summaries(explorer_path, contexts_path):
    """Generate intelligence summaries per theme."""
    print("\n=== GENERATING INTELLIGENCE SUMMARIES ===")
    explorer = json.load(open(explorer_path))
    contexts = json.load(open(contexts_path))
    markets = explorer["markets"]

    summaries = {}

    for theme in THEMES:
        theme_markets = [m for m in markets if theme in (m.get("themes") or []) and (m.get("volume_7d") or 0) >= MIN_VOLUME_MOVES]
        if len(theme_markets) < 3:
            continue

        # Group and build prompt data
        theme_markets.sort(key=lambda m: m.get("volume_7d") or 0, reverse=True)
        top = theme_markets[:25]
        market_data = ""
        for i, m in enumerate(top):
            p = m.get("price") or 0
            v = m.get("volume_7d") or 0
            market_data += f"\nGROUP {i+1}. {m['question']}\n   Probability: {p*100:.1f}%  |  Volume: ${v:,.0f}\n"

        ctx = contexts["themes"].get(theme, {}).get("context", "")
        prompt = f"""You are a research analyst writing a market intelligence briefing for institutional portfolio managers.

Given the following prediction market data for the {theme} sector, produce 3-8 bullet points. Each bullet should state what the market is pricing, include the probability, and include a confidence indicator: HIGH (>$500K volume), MEDIUM ($100K-$500K), or LOW (<$100K).

Include which GROUP numbers you used for each bullet.

Current context for {theme}: {ctx}

=== MARKET DATA ===
{market_data}

Output ONLY a JSON array of objects with "text", "confidence", and "groups" fields."""

        result = call_claude(prompt)
        if result:
            try:
                bullets = json.loads(result.strip())
                if isinstance(bullets, list):
                    # Resolve group numbers to source questions
                    for b in bullets:
                        source_qs = []
                        for g in b.get("groups", []):
                            if 1 <= g <= len(top):
                                source_qs.append(top[g-1]["question"])
                        b["source_questions"] = source_qs

                    summaries[theme] = {
                        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "bullets": bullets,
                    }
                    print(f"  {theme}: {len(bullets)} bullets")
            except Exception as e:
                print(f"  {theme}: parse error: {e}")

    explorer["theme_summaries"] = summaries
    with open(explorer_path, "w") as f:
        json.dump(explorer, f)
    print(f"  Saved summaries for {len(summaries)} themes")


def generate_moves(explorer_path, contexts_path):
    """Generate 24h/7d moves narratives per theme."""
    print("\n=== GENERATING MOVES NARRATIVES ===")
    explorer = json.load(open(explorer_path))
    contexts = json.load(open(contexts_path))
    markets = explorer["markets"]

    for period, field in [("24h", "change_24h"), ("7d", "change_7d")]:
        moves = {}
        for theme in THEMES:
            themed = [m for m in markets
                      if theme in (m.get("themes") or [])
                      and m.get(field) is not None
                      and abs(m[field]) > 0.5
                      and (m.get("volume_7d") or 0) >= MIN_VOLUME_MOVES]

            if not themed:
                continue

            themed.sort(key=lambda m: abs(m[field]), reverse=True)
            top = themed[:8]

            moves_data = ""
            for m in top:
                c = m[field]
                p = m.get("price", 0) or 0
                v = m.get("volume_7d", 0) or 0
                moves_data += f"  {c:>+6.1f}pp -> now {p*100:.1f}%  (${v:,.0f} vol)  {m['question'][:80]}\n"

            ctx = contexts["themes"].get(theme, {}).get("context", "")
            interval_label = "last 24 hours" if period == "24h" else "last 7 days"

            prompt = f"""You are writing compact market move summaries for institutional portfolio managers about {theme} prediction markets.

For each market move below, write a SHORT natural language interpretation (max 15 words) that explains what the move means. Include the direction and current probability naturally in the text.

Current {theme} context: {ctx}

Top movers ({interval_label}, all with >$50K weekly volume):
{moves_data}

Output a JSON array of objects with "text", "change" (number), "price" (number 0-1), and "question" (original question string). Max 5 items.
Output ONLY the JSON array."""

            result = call_claude(prompt)
            if result:
                try:
                    movers = json.loads(result.strip())
                    if isinstance(movers, list):
                        moves[theme] = {"movers": movers}
                        print(f"  {period} {theme}: {len(movers)} movers")
                except Exception as e:
                    print(f"  {period} {theme}: parse error: {e}")

        explorer[f"theme_moves_{period}"] = moves

    with open(explorer_path, "w") as f:
        json.dump(explorer, f)
    print(f"  Saved moves data")


def main():
    print("=" * 60)
    print("Prediction Markets V2 — API Classification + Insights")
    print("=" * 60)

    base = os.path.dirname(os.path.abspath(__file__))
    explorer_path = os.path.join(base, "explorer_data.json")
    contexts_path = os.path.join(base, "theme_contexts.json")
    overrides_path = os.path.join(base, "theme_overrides.json")

    if not API_KEY:
        print("WARNING: ANTHROPIC_API_KEY not set. Skipping LLM steps.")
        return

    # Step 1: Classify new markets
    classify_new_markets(explorer_path, contexts_path, overrides_path)

    # Re-run extract_themes to apply new overrides
    print("\n=== RE-APPLYING THEMES ===")
    explorer = json.load(open(explorer_path))
    overrides = json.load(open(overrides_path))
    contexts = json.load(open(contexts_path))
    theme_defs = contexts["themes"]

    for m in explorer["markets"]:
        m["themes"] = overrides.get(m["question"], ["Other"])
        # Sub-tags
        ql = m["question"].lower()
        sub_tags = []
        for theme in m["themes"]:
            if theme == "Other":
                continue
            tdef = theme_defs.get(theme)
            if not tdef:
                continue
            for sub_name, keywords in tdef.get("sub_keywords", {}).items():
                for kw in keywords:
                    if kw.lower() in ql:
                        sub_tags.append(sub_name)
                        break
        seen = set()
        m["sub_tags"] = [s for s in sub_tags if not (s in seen or seen.add(s))]

    with open(explorer_path, "w") as f:
        json.dump(explorer, f)

    # Step 2: Generate intelligence summaries
    generate_summaries(explorer_path, contexts_path)

    # Step 3: Generate moves narratives
    generate_moves(explorer_path, contexts_path)

    print("\n=== DONE ===")


if __name__ == "__main__":
    main()
