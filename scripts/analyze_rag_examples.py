#!/usr/bin/env python3
"""
Analyze RAG examples to understand:
1. How many unique schemas (source_files) we have
2. How many examples per schema
3. Quality variation in intents
"""

import json
from pathlib import Path
from collections import defaultdict

def main():
    examples_dir = Path(__file__).parent.parent / "nl2protocol" / "examples"

    # Group by source_file
    by_source = defaultdict(list)

    for f in examples_dir.glob("ex_*.json"):
        try:
            data = json.loads(f.read_text())
            source = data.get("source_file", "unknown")
            by_source[source].append({
                "file": f.name,
                "id": data.get("id"),
                "style": data.get("style"),
                "intent": data.get("intent", "")[:100] + "..." if len(data.get("intent", "")) > 100 else data.get("intent", "")
            })
        except Exception as e:
            print(f"Error reading {f.name}: {e}")

    print(f"Total example files: {sum(len(v) for v in by_source.values())}")
    print(f"Unique source files (schemas): {len(by_source)}")
    print(f"Average examples per schema: {sum(len(v) for v in by_source.values()) / len(by_source):.1f}")

    # Show distribution
    counts = defaultdict(int)
    for source, examples in by_source.items():
        counts[len(examples)] += 1

    print(f"\nDistribution of examples per schema:")
    for count, num_schemas in sorted(counts.items()):
        print(f"  {count} examples: {num_schemas} schemas")

    # Show a few examples of different styles for same schema
    print(f"\n" + "=" * 60)
    print("SAMPLE: Same schema, different styles")
    print("=" * 60)

    for source, examples in list(by_source.items())[:2]:
        print(f"\nSource: {source}")
        for ex in examples:
            print(f"  [{ex['style']}] {ex['intent']}")

if __name__ == "__main__":
    main()
