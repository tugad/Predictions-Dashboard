"""
Prediction Markets V2 — Theme Extraction
==========================================
Reads markets_raw.json and classifies each market into themes using
LLM-based classification with per-theme context from theme_contexts.json.

Uses theme_overrides.json as a cache — only new/unseen markets get classified.
Assigns sub-tags via keyword matching after primary classification.

Outputs explorer_data.json.

NOTE: This script is designed to be run via Claude Code, which acts as
the LLM for classification. When run standalone, it will only apply
cached overrides and keyword-based sub-tags — new markets will be tagged
"Unclassified" and need a manual classification pass.

See PROJECT.md section 5 for full specification.
"""

import json
import re
import time
import sys


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def assign_sub_tags(question, themes, theme_defs):
    """Assign sub-tags by keyword matching within assigned themes."""
    ql = question.lower()
    sub_tags = []
    for theme in themes:
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
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in sub_tags:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def build_classification_prompt(batch, theme_contexts, date):
    """Build the LLM prompt for a batch of markets."""
    theme_block = ""
    for name, tdef in theme_contexts.items():
        theme_block += f"\n{name.upper()}: {tdef['context']}\n"

    market_list = ""
    for i, q in enumerate(batch):
        market_list += f"{i}: {q}\n"

    return f"""You are tagging prediction markets for institutional portfolio managers.
Assign one or more themes from the list below based on relevance to that
sector. If a market is not relevant to institutional portfolio managers
(sports, weather, entertainment, baby names, social media, temperature,
app rankings, song rankings, TV shows, YouTubers, etc.), tag it as "Other".

A market CAN have multiple themes when it's relevant to more than one.
For example, Iran conflict markets are both "Geopolitics" and "Energies"
because the Hormuz closure directly impacts oil supply.

Available themes: Energies, Rates, Equities, Crypto, Geopolitics, Commodities, Macro, Politics, Other

=== THEME CONTEXTS (current as of {date}) ===
{theme_block}
=== MARKETS TO CLASSIFY ===

{market_list}
=== OUTPUT ===

Output ONLY a Python dictionary mapping index number to a list of theme
strings. No explanation needed.
"""


def main():
    print("=" * 60)
    print("Prediction Markets V2 — Theme Extraction")
    print("=" * 60)

    # Load inputs
    raw = load_json("markets_raw.json")
    if not raw:
        print("ERROR: markets_raw.json not found. Run fetch_markets.py first.")
        sys.exit(1)

    contexts_data = load_json("theme_contexts.json")
    if not contexts_data:
        print("ERROR: theme_contexts.json not found.")
        sys.exit(1)

    overrides = load_json("theme_overrides.json", default={})
    theme_defs = contexts_data["themes"]
    date = contexts_data.get("updated_at", "unknown")

    markets = raw["markets"]
    spot_prices = raw.get("spot_prices", {})
    print(f"\n  Markets to process: {len(markets)}")
    print(f"  Cached overrides: {len(overrides)}")

    # Split into cached vs new
    cached_count = 0
    new_questions = []
    new_indices = []

    for i, m in enumerate(markets):
        q = m["question"]
        if q in overrides:
            m["themes"] = overrides[q]
            cached_count += 1
        else:
            new_questions.append(q)
            new_indices.append(i)

    print(f"  Already cached: {cached_count}")
    print(f"  New (need classification): {len(new_questions)}")

    # For new markets: if running interactively via Claude Code, the agent
    # will process these. If running standalone, tag as "Unclassified".
    if new_questions:
        print(f"\n  --- Classification needed for {len(new_questions)} markets ---")
        print(f"  Run this script via Claude Code for LLM classification.")
        print(f"  For now, tagging new markets as 'Unclassified'.")
        print(f"  These will appear under 'Other' in the dashboard.\n")

        # Batch the prompt for manual/agent use
        batches = []
        batch_size = 100
        for start in range(0, len(new_questions), batch_size):
            batch = new_questions[start:start + batch_size]
            prompt = build_classification_prompt(batch, theme_defs, date)
            batches.append({
                "start_idx": start,
                "count": len(batch),
                "prompt": prompt,
            })

        # Save prompts for manual use
        save_json("classification_prompts.json", batches)
        print(f"  Saved {len(batches)} classification prompts to classification_prompts.json")
        print(f"  Each prompt can be sent to an LLM for classification.\n")

        # Tag as Unclassified for now
        for idx in new_indices:
            markets[idx]["themes"] = ["Other"]
            overrides[markets[idx]["question"]] = ["Other"]

    # Assign sub-tags for all markets
    print("  Assigning sub-tags...")
    for m in markets:
        m["sub_tags"] = assign_sub_tags(m["question"], m["themes"] or [], theme_defs)

    # Theme summary
    from collections import Counter
    theme_counts = Counter()
    for m in markets:
        for t in (m["themes"] or []):
            theme_counts[t] += 1

    print(f"\n  Theme distribution:")
    for t, c in theme_counts.most_common():
        print(f"    {t}: {c}")

    # Save overrides
    save_json("theme_overrides.json", overrides)
    print(f"\n  Updated theme_overrides.json ({len(overrides)} entries)")

    # Build final output
    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "spot_prices": spot_prices,
        "markets": markets,
    }

    save_json("explorer_data.json", output)
    size_mb = len(json.dumps(output)) / 1024 / 1024
    print(f"  Saved explorer_data.json ({size_mb:.1f} MB, {len(markets)} markets)")
    print("  Done.")


if __name__ == "__main__":
    main()
