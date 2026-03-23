#!/usr/bin/env python3
"""
run_pipeline.py

Main data curation pipeline:
1. Load Opentrons protocol scripts (from local dir or GitHub)
2. Parse each script to extract schema
3. Generate 3 NL intent variations per script
4. Save (intent, schema) pairs to examples/ directory

Usage:
    python run_pipeline.py --input ./protocols/ --output ../examples/
    python run_pipeline.py --github opentrons/Protocols --limit 50
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
import hashlib

from script_parser import parse_protocol_script, parse_protocol_source
from intent_generator import IntentGenerator


def generate_example_id(intent: str, style: str) -> str:
    """Generate a unique ID for an example."""
    hash_input = f"{intent}:{style}"
    short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]
    return f"ex_{style}_{short_hash}"


def process_script(
    script_path: str,
    generator: IntentGenerator,
    verbose: bool = False
) -> List[Dict[str, Any]]:
    """
    Process a single script and return list of (intent, schema) examples.

    Returns:
        List of 3 examples (one per style), or empty list on failure
    """
    if verbose:
        print(f"  Parsing: {script_path}")

    # Parse script to schema
    schema = parse_protocol_script(script_path)
    if not schema:
        if verbose:
            print(f"    SKIP: Could not parse")
        return []

    # Read raw script for intent generation
    with open(script_path, 'r') as f:
        raw_script = f.read()

    if verbose:
        print(f"    Generating intents...")

    # Generate 3 intent variations
    intents = generator.generate_multi_style(raw_script)
    if not intents:
        if verbose:
            print(f"    SKIP: Could not generate intents")
        return []

    # Build output schema (without raw_script to save space)
    output_schema = {
        "protocol_name": schema.get("protocol_name", ""),
        "author": schema.get("author", ""),
        "labware": schema.get("labware", []),
        "pipettes": schema.get("pipettes", []),
        "modules": schema.get("modules", []),
        "commands": schema.get("commands", [])
    }

    # Create 3 examples
    examples = []
    styles = ["technical", "casual", "concise"]

    for style in styles:
        intent = intents.get(style, "")
        if not intent:
            continue

        example = {
            "id": generate_example_id(intent, style),
            "intent": intent,
            "style": style,
            "source_file": os.path.basename(script_path),
            "output_schema": output_schema
        }
        examples.append(example)

    if verbose:
        print(f"    Generated {len(examples)} examples")

    return examples


def get_processed_sources(output_dir: str) -> set:
    """Get set of source files that have already been processed."""
    processed = set()
    output_path = Path(output_dir)

    if not output_path.exists():
        return processed

    for json_file in output_path.glob("ex_*.json"):
        try:
            with open(json_file, 'r') as f:
                example = json.load(f)
                source = example.get("source_file", "")
                if source:
                    processed.add(source)
        except Exception:
            continue

    return processed


def process_directory(
    input_dir: str,
    output_dir: str,
    limit: Optional[int] = None,
    verbose: bool = False
) -> int:
    """
    Process all Python scripts in a directory.

    Returns:
        Number of examples generated
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    # Get already processed source files
    already_processed = get_processed_sources(output_dir)
    if already_processed:
        print(f"Skipping {len(already_processed)} already processed scripts")

    # Find all Python files
    scripts = list(input_path.glob("**/*.py"))

    # Filter to likely protocol files
    scripts = [s for s in scripts if is_likely_protocol(s)]

    # Skip already processed files
    scripts = [s for s in scripts if s.name not in already_processed]

    if limit:
        scripts = scripts[:limit]

    print(f"Found {len(scripts)} new protocol scripts to process")

    generator = IntentGenerator()
    total_examples = 0

    for i, script_path in enumerate(scripts):
        print(f"[{i+1}/{len(scripts)}] {script_path.name}")

        examples = process_script(str(script_path), generator, verbose)

        for example in examples:
            # Save each example as JSON
            out_file = output_path / f"{example['id']}.json"
            with open(out_file, 'w') as f:
                json.dump(example, f, indent=2)
            total_examples += 1

    return total_examples


def is_likely_protocol(path: Path) -> bool:
    """Check if a Python file is likely an Opentrons protocol."""
    try:
        content = path.read_text()

        # Must have protocol indicators
        indicators = [
            "def run(protocol",
            "protocol_api",
            "load_labware",
            "load_instrument",
        ]

        # Check for at least 2 indicators
        matches = sum(1 for ind in indicators if ind in content)
        return matches >= 2

    except Exception:
        return False


def fetch_from_github(repo: str, output_dir: str, limit: int = 50) -> str:
    """
    Fetch protocol files from a GitHub repo.

    Args:
        repo: GitHub repo in format "owner/repo"
        output_dir: Where to save downloaded files
        limit: Max files to download

    Returns:
        Path to downloaded files directory
    """
    import urllib.request
    import zipfile
    import tempfile

    print(f"Fetching from GitHub: {repo}")

    # Download repo as zip
    zip_url = f"https://github.com/{repo}/archive/refs/heads/main.zip"

    temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(temp_dir, "repo.zip")

    print(f"Downloading {zip_url}...")
    urllib.request.urlretrieve(zip_url, zip_path)

    # Extract
    print("Extracting...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(temp_dir)

    # Find extracted folder
    extracted = [d for d in os.listdir(temp_dir) if os.path.isdir(os.path.join(temp_dir, d))]
    if extracted:
        return os.path.join(temp_dir, extracted[0])

    return temp_dir


def main():
    parser = argparse.ArgumentParser(
        description="Generate (intent, schema) training data from Opentrons protocols"
    )

    parser.add_argument(
        "--input", "-i",
        help="Input directory containing protocol .py files"
    )

    parser.add_argument(
        "--github", "-g",
        help="GitHub repo to fetch protocols from (e.g., 'Opentrons/Protocols')"
    )

    parser.add_argument(
        "--output", "-o",
        default="../examples",
        help="Output directory for generated examples (default: ../examples)"
    )

    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Limit number of scripts to process"
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed progress"
    )

    args = parser.parse_args()

    if not args.input and not args.github:
        parser.error("Either --input or --github is required")

    # Determine input directory
    if args.github:
        input_dir = fetch_from_github(args.github, args.output, args.limit or 50)
    else:
        input_dir = args.input

    # Process
    total = process_directory(
        input_dir,
        args.output,
        limit=args.limit,
        verbose=args.verbose
    )

    print(f"\nDone! Generated {total} examples in {args.output}")


if __name__ == "__main__":
    main()
