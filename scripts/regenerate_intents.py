#!/usr/bin/env python3
"""
Regenerate high-quality scientist-style intents for RAG examples.

1. Deduplicates to 1 example per schema (source_file)
2. Uses Claude to generate realistic scientist instructions
3. Backs up originals and saves regenerated examples
"""

import json
import os
import time
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import Optional
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

SCIENTIST_PROMPT = """You are a scientist writing instructions for a lab robot protocol.

Given this Opentrons protocol schema, write a natural language instruction that a scientist would realistically give.

Guidelines:
- Be specific about volumes (e.g., "100µL" not "some liquid")
- Reference specific wells when the protocol does (e.g., "wells A1-A12")
- Mention the purpose/goal if inferrable from the protocol name or steps
- Include temperatures, timing, incubation periods if present
- Mention modules being used (temperature control, magnetic separation, etc.)
- Use natural scientist language, not robotic commands
- Be concise but complete - 1-3 sentences typically

Protocol Schema:
{schema}

Write the scientist's instruction (just the instruction, no preamble):"""


def extract_schema_summary(output_schema: dict) -> str:
    """Extract relevant parts of schema for the prompt."""
    summary = {
        "protocol_name": output_schema.get("protocol_name", "Unknown"),
        "labware": output_schema.get("labware", []),
        "pipettes": output_schema.get("pipettes", []),
        "modules": output_schema.get("modules", []),
        # Include first 20 commands to keep prompt reasonable
        "commands": output_schema.get("commands", [])[:20]
    }

    # Add note if commands were truncated
    total_commands = len(output_schema.get("commands", []))
    if total_commands > 20:
        summary["_note"] = f"Showing 20 of {total_commands} commands"

    return json.dumps(summary, indent=2)


def generate_intent(client: Anthropic, output_schema: dict, model: str = "claude-sonnet-4-20250514") -> Optional[str]:
    """Generate a scientist-style intent from protocol schema."""
    schema_summary = extract_schema_summary(output_schema)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": SCIENTIST_PROMPT.format(schema=schema_summary)
            }]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"  Error generating intent: {e}")
        return None


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Regenerate RAG example intents")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files, just show what would happen")
    parser.add_argument("--limit", type=int, default=None, help="Process only N schemas (for testing)")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Model to use")
    args = parser.parse_args()

    examples_dir = Path(__file__).parent.parent / "nl2protocol" / "examples"
    backup_dir = Path(__file__).parent.parent / "nl2protocol" / "examples_backup"

    # Check API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY not set")
        return 1

    client = Anthropic(api_key=api_key)

    # Group examples by source_file
    by_source = defaultdict(list)
    for f in examples_dir.glob("ex_*.json"):
        try:
            data = json.loads(f.read_text())
            source = data.get("source_file", "unknown")
            by_source[source].append({"path": f, "data": data})
        except Exception as e:
            print(f"Error reading {f.name}: {e}")

    print(f"Found {len(by_source)} unique schemas")

    if args.limit:
        sources = list(by_source.keys())[:args.limit]
        by_source = {k: by_source[k] for k in sources}
        print(f"Limited to {len(by_source)} schemas")

    if not args.dry_run:
        # Create backup
        if not backup_dir.exists():
            print(f"Creating backup at {backup_dir}")
            shutil.copytree(examples_dir, backup_dir)
        else:
            print(f"Backup already exists at {backup_dir}")

    # Process each schema
    processed = 0
    errors = 0

    for source_file, examples in by_source.items():
        print(f"\n[{processed + 1}/{len(by_source)}] {source_file}")

        # Pick the first example to keep (prefer 'technical' style if available)
        keep = None
        for ex in examples:
            if ex["data"].get("style") == "technical":
                keep = ex
                break
        if not keep:
            keep = examples[0]

        # Files to delete (duplicates)
        to_delete = [ex["path"] for ex in examples if ex["path"] != keep["path"]]

        print(f"  Keeping: {keep['path'].name}")
        print(f"  Deleting: {len(to_delete)} duplicates")

        # Generate new intent
        output_schema = keep["data"].get("output_schema", {})
        if not output_schema:
            print(f"  WARNING: No output_schema, skipping regeneration")
            errors += 1
            continue

        print(f"  Generating new intent...")
        new_intent = generate_intent(client, output_schema, model=args.model)

        if not new_intent:
            print(f"  ERROR: Failed to generate intent")
            errors += 1
            continue

        # Show old vs new
        old_intent = keep["data"].get("intent", "")[:80]
        print(f"  Old: {old_intent}...")
        print(f"  New: {new_intent[:80]}...")

        if not args.dry_run:
            # Update the kept file
            keep["data"]["intent"] = new_intent
            keep["data"]["style"] = "scientist"  # New style
            keep["data"].pop("config", None)  # Will regenerate if needed

            keep["path"].write_text(json.dumps(keep["data"], indent=2))

            # Delete duplicates
            for path in to_delete:
                path.unlink()

        processed += 1

        # Rate limiting - be nice to the API
        time.sleep(0.5)

    print(f"\n{'=' * 60}")
    print(f"SUMMARY")
    print(f"{'=' * 60}")
    print(f"Processed: {processed}")
    print(f"Errors: {errors}")
    if args.dry_run:
        print(f"(Dry run - no files modified)")
    else:
        print(f"Backup at: {backup_dir}")
        print(f"Examples reduced from ~{sum(len(v) for v in by_source.values())} to {processed}")

    return 0


if __name__ == "__main__":
    exit(main())
