"""
Prediction Markets V2 — Theme Intelligence Summaries
======================================================
Reads explorer_data.json + theme_contexts.json.
For each theme, groups High/Med quality markets and generates
natural language summary bullets via Claude agents.

Updates explorer_data.json with a theme_summaries key.

See PROJECT.md section 5.6 for full specification.
"""

import json
import re
import time
from collections import defaultdict


def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


THEMES = ['Energies', 'Rates', 'Equities', 'Crypto', 'Geopolitics', 'Commodities', 'Macro', 'Elections', 'Sports']
MIN_VOLUME = 50000  # High + Med threshold


def group_markets(markets):
    """Group related markets by event_ticker or question prefix similarity."""
    # First group by event_ticker if available
    by_event = defaultdict(list)
    no_event = []

    for m in markets:
        evt = m.get("event_ticker")
        if evt:
            by_event[evt].append(m)
        else:
            no_event.append(m)

    # For no-event markets, group by question prefix (first 40 chars or up to "by")
    prefix_groups = defaultdict(list)
    for m in no_event:
        q = m["question"]
        # Try to find a natural grouping point
        prefix = q
        for sep in [" by ", " before ", " in 202", " on 202"]:
            idx = q.lower().find(sep)
            if idx > 15:
                prefix = q[:idx]
                break
        if len(prefix) > 60:
            prefix = prefix[:60]
        prefix_groups[prefix].append(m)

    # Merge into final groups
    groups = []
    for evt, mlist in by_event.items():
        rep = max(mlist, key=lambda m: m.get("volume_7d") or 0)
        groups.append({
            "question": rep["question"],
            "probability": rep.get("price") or 0,
            "volume": sum(m.get("volume_7d") or 0 for m in mlist),
            "market_count": len(mlist),
            "venue": rep.get("venue", ""),
            "end_dates": sorted(set(m.get("end_date", "") for m in mlist)),
            "variants": [{"q": m["question"], "prob": m.get("price") or 0,
                          "vol": m.get("volume_7d") or 0, "end": m.get("end_date", "")}
                         for m in sorted(mlist, key=lambda x: x.get("end_date", ""))],
        })

    for prefix, mlist in prefix_groups.items():
        if len(mlist) == 1:
            m = mlist[0]
            groups.append({
                "question": m["question"],
                "probability": m.get("price") or 0,
                "volume": m.get("volume_7d") or 0,
                "market_count": 1,
                "venue": m.get("venue", ""),
                "end_dates": [m.get("end_date", "")],
                "variants": [],
            })
        else:
            rep = max(mlist, key=lambda m: m.get("volume_7d") or 0)
            groups.append({
                "question": rep["question"],
                "probability": rep.get("price") or 0,
                "volume": sum(m.get("volume_7d") or 0 for m in mlist),
                "market_count": len(mlist),
                "venue": rep.get("venue", ""),
                "end_dates": sorted(set(m.get("end_date", "") for m in mlist)),
                "variants": [{"q": m["question"], "prob": m.get("price") or 0,
                              "vol": m.get("volume_7d") or 0, "end": m.get("end_date", "")}
                             for m in sorted(mlist, key=lambda x: x.get("end_date", ""))],
            })

    # Sort by combined volume
    groups.sort(key=lambda g: g["volume"], reverse=True)
    return groups


def build_summary_prompt(theme, context, groups):
    """Build the LLM prompt for generating summary bullets."""
    market_data = ""
    used_groups = groups[:25]  # Top 25 groups by volume
    for i, g in enumerate(used_groups):
        market_data += f"\nGROUP {i+1}. {g['question']}\n"
        market_data += f"   Probability: {g['probability']*100:.1f}%  |  Combined volume: ${g['volume']:,.0f}  |  Markets: {g['market_count']}\n"
        if g["variants"] and len(g["variants"]) > 1:
            market_data += "   Timeline:\n"
            for v in g["variants"][:6]:
                market_data += f"     - {v['end']}: {v['prob']*100:.1f}% (${v['vol']:,.0f})\n"

    prompt = f"""You are a research analyst writing a market intelligence briefing for institutional portfolio managers.

Given the following prediction market data for the {theme} sector, produce 3-8 bullet points summarizing the key signals. Each bullet should:
- State what the market is pricing in plain language
- Include the probability as a percentage
- Include a confidence indicator: HIGH (multiple markets, >$500K combined volume), MEDIUM ($100K-$500K or single high-volume market), or LOW (<$100K)
- If there's a timeline (same event at different dates), mention the probability curve across dates
- Include which GROUP numbers you used to produce this bullet

Do NOT just list markets. Synthesize — combine related signals into single insights. Lead with the most actionable or surprising signals.

Current context for {theme}: {context}

=== MARKET DATA ===
{market_data}

=== OUTPUT FORMAT ===
Output a JSON array of objects, each with:
- "text": the bullet point text (plain language, 1-2 sentences)
- "confidence": "HIGH", "MEDIUM", or "LOW"
- "groups": array of group numbers (integers) that this bullet synthesizes

Output ONLY the JSON array, no explanation.
"""
    return prompt, used_groups


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Theme Intelligence Summaries")
    print("=" * 60)

    explorer = load_json("explorer_data.json")
    contexts = load_json("theme_contexts.json")
    markets = explorer["markets"]

    summaries = {}
    prompts_to_run = {}

    for theme in THEMES:
        # Collect High + Med markets for this theme
        theme_markets = [
            m for m in markets
            if theme in (m.get("themes") or [])
            and (m.get("volume_7d") or 0) >= MIN_VOLUME
        ]

        print(f"\n  {theme}: {len(theme_markets)} High/Med markets")

        if len(theme_markets) == 0:
            summaries[theme] = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "bullets": [{"text": "No high-confidence markets in this theme.", "confidence": "LOW"}],
            }
            continue

        # Group related markets
        groups = group_markets(theme_markets)
        print(f"    Grouped into {len(groups)} signal clusters")

        # Build prompt
        ctx = contexts["themes"].get(theme, {}).get("context", "")
        prompt, used_groups = build_summary_prompt(theme, ctx, groups)

        # Build group → source questions mapping
        group_sources = {}
        for i, g in enumerate(used_groups):
            questions = [g["question"]]
            for v in g.get("variants", []):
                if v["q"] not in questions:
                    questions.append(v["q"])
            group_sources[i + 1] = questions  # 1-indexed to match prompt

        # Save prompt for agent execution
        prompts_to_run[theme] = {
            "prompt": prompt,
            "group_count": len(groups),
            "market_count": len(theme_markets),
            "group_sources": group_sources,
        }

    # Save prompts and group mappings
    save_json("summary_prompts.json", prompts_to_run)
    print(f"\n  Saved {len(prompts_to_run)} summary prompts to summary_prompts.json")

    # Save the prompt text files for agent consumption
    for theme, data in prompts_to_run.items():
        fname = f"/tmp/summary_{theme.lower().replace(' ', '_')}.txt"
        with open(fname, "w") as f:
            f.write(data["prompt"])
        print(f"  Wrote {fname}")


if __name__ == "__main__":
    main()
