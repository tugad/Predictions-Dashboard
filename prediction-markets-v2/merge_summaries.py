"""
Merge summary JSON files into explorer_data.json.
Resolves group numbers to source market questions using summary_prompts.json.
"""

import json
import glob
import time


def load_json(path):
    with open(path) as f:
        return json.load(f)


def main():
    explorer = load_json("explorer_data.json")
    prompts = load_json("summary_prompts.json")
    summaries = {}

    theme_map = {
        "energies": "Energies", "rates": "Rates", "equities": "Equities",
        "crypto": "Crypto", "geopolitics": "Geopolitics", "commodities": "Commodities",
        "macro": "Macro", "elections": "Elections", "sports": "Sports",
        "politics": "Elections",  # Legacy alias
    }

    for f in sorted(glob.glob("summary_*.json")):
        if "prompts" in f:
            continue
        key = f.replace("summary_", "").replace(".json", "")
        theme = theme_map.get(key)
        if not theme:
            continue

        bullets = load_json(f)
        if not isinstance(bullets, list):
            print(f"  {theme}: unexpected format, skipping")
            continue

        # Get group_sources for this theme
        group_sources = prompts.get(theme, {}).get("group_sources", {})

        # Resolve group numbers → source questions for each bullet
        for bullet in bullets:
            groups = bullet.get("groups", [])
            source_questions = []
            for g in groups:
                qs = group_sources.get(str(g), [])
                for q in qs:
                    if q not in source_questions:
                        source_questions.append(q)
            bullet["source_questions"] = source_questions

        summaries[theme] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bullets": bullets,
        }

        total_sources = sum(len(b.get("source_questions", [])) for b in bullets)
        print(f"  {theme}: {len(bullets)} bullets, {total_sources} source markets linked")

    explorer["theme_summaries"] = summaries

    with open("explorer_data.json", "w") as f:
        json.dump(explorer, f)

    print(f"\nUpdated explorer_data.json with {len(summaries)} theme summaries")


if __name__ == "__main__":
    main()
