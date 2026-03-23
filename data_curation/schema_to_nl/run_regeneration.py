#!/usr/bin/env python3
"""
RAG Example NL Regeneration Pipeline

1. Deduplicates examples (1 per schema/source_file)
2. Uses rule-based conversion to generate precise NL
3. Optionally uses LLM to polish into natural scientist language
4. Saves updated examples with both NL variants
"""

import json
import os
import sys
import shutil
import time
from pathlib import Path
from collections import defaultdict
from typing import Dict, List, Optional, Tuple
from datetime import datetime

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from data_curation.schema_to_nl.pattern_detector import PatternDetector
from data_curation.schema_to_nl.schema_analyzer import SchemaAnalyzer
from data_curation.schema_to_nl.nl_generator import NLGenerator


def has_unresolved_variables(schema: Dict) -> bool:
    """
    Check if schema contains unresolved variable placeholders.

    These look like "reservoir_loader:slot", "pip_name", "mm_vol", etc.
    They come from protocols with runtime variables that weren't resolved.
    """
    def check_value(val) -> bool:
        if isinstance(val, str):
            # Check for patterns like "name:property" or common placeholder names
            if ":" in val and not val.startswith("http"):
                return True
            # Check for common placeholder patterns (variable names)
            placeholder_patterns = [
                "_loader", "_mount", "_name", "_vol", "_height",
                "pipmnt", "pip_name", "tip_name", "max_vol", "mm_vol",
                "mag_height", "bead_vol", "aspirate_vol", "reag_vol"
            ]
            val_lower = val.lower()
            for pattern in placeholder_patterns:
                if pattern in val_lower and not val_lower.startswith("p") and not "_gen" in val_lower:
                    # Allow actual pipette names like p300_single_gen2
                    return True
        return False

    def check_dict(d: Dict) -> bool:
        for key, val in d.items():
            if check_value(val):
                return True
            if isinstance(val, dict):
                if check_dict(val):
                    return True
            if isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        if check_dict(item):
                            return True
                    elif check_value(item):
                        return True
        return False

    # Check labware slots and load_names
    for lw in schema.get("labware", []):
        if check_value(lw.get("slot", "")):
            return True
        if check_value(lw.get("load_name", "")):
            return True

    # Check pipette mounts and models
    for pip in schema.get("pipettes", []):
        if check_value(pip.get("mount", "")):
            return True
        if check_value(pip.get("model", "")):
            return True

    # Check module slots and types
    for mod in schema.get("modules", []):
        if check_value(mod.get("slot", "")):
            return True
        if check_value(mod.get("module_type", "")):
            return True

    # Check commands for variable volumes/heights
    for cmd in schema.get("commands", []):
        vol = cmd.get("volume")
        if vol and check_value(str(vol)):
            return True
        height = cmd.get("height")
        if height and check_value(str(height)):
            return True

    return False


def generate_precise_nl(schema: Dict, verbose: bool = False) -> Tuple[str, str]:
    """
    Generate precise and concise NL from schema.

    Returns:
        (precise_nl, concise_nl)
    """
    try:
        # Analyze schema
        analyzer = SchemaAnalyzer(schema)
        analysis = analyzer.analyze()

        # Detect patterns
        detector = PatternDetector(schema)
        patterns = detector.detect_patterns()

        # Generate NL
        generator = NLGenerator(analysis, patterns)
        precise_nl = generator.generate_precise_nl()
        concise_nl = generator.generate_concise_nl()

        return precise_nl, concise_nl

    except Exception as e:
        if verbose:
            import traceback
            print(f"    Error generating NL: {e}")
            traceback.print_exc()
        else:
            print(f"    Error generating NL: {e}")
        return "", ""


def polish_with_llm(precise_nl: str, schema: Dict, client, model: str) -> Optional[str]:
    """
    Use LLM to polish precise NL into natural scientist language.

    This step is optional and creates a more natural-sounding instruction
    while preserving the factual content from the precise NL.
    """
    prompt = f"""You are a scientist writing instructions for a lab protocol.

Given this precise protocol description, rewrite it as a natural instruction that a scientist would give to a technician or automated system. Keep all the specific details (volumes, wells, temperatures) but make it sound more natural and conversational.

Precise description:
{precise_nl}

Protocol name: {schema.get('protocol_name', 'Unknown')}

Write a natural scientist instruction (2-4 sentences, preserve all specific details). Do NOT wrap your response in quotes:"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=500,  # Increased from 300 to avoid truncation
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text.strip()

        # Strip surrounding quotes if present
        if result.startswith('"') and result.endswith('"'):
            result = result[1:-1]
        elif result.startswith("'") and result.endswith("'"):
            result = result[1:-1]

        return result
    except Exception as e:
        print(f"    LLM polish error: {e}")
        return None


def process_examples(
    examples_dir: Path,
    output_dir: Path,
    backup_dir: Path,
    use_llm: bool = False,
    model: str = "claude-sonnet-4-20250514",
    limit: Optional[int] = None,
    dry_run: bool = False,
    verbose: bool = False
) -> Dict:
    """
    Process all examples:
    1. Deduplicate to 1 per source_file
    2. Generate precise NL
    3. Optionally polish with LLM
    4. Save results
    """
    # Initialize LLM client if needed
    client = None
    if use_llm:
        try:
            from anthropic import Anthropic
            from dotenv import load_dotenv
            load_dotenv()
            api_key = os.getenv("ANTHROPIC_API_KEY")
            if api_key:
                client = Anthropic(api_key=api_key)
            else:
                print("Warning: ANTHROPIC_API_KEY not set, skipping LLM polish")
                use_llm = False
        except ImportError:
            print("Warning: anthropic not installed, skipping LLM polish")
            use_llm = False

    # Group examples by source_file
    by_source = defaultdict(list)
    for f in examples_dir.glob("ex_*.json"):
        try:
            data = json.loads(f.read_text())
            source = data.get("source_file", "unknown")
            by_source[source].append({"path": f, "data": data})
        except Exception as e:
            print(f"Error reading {f.name}: {e}")

    print(f"Found {len(by_source)} unique schemas from {sum(len(v) for v in by_source.values())} examples")

    if limit:
        sources = list(by_source.keys())[:limit]
        by_source = {k: by_source[k] for k in sources}
        print(f"Limited to {len(by_source)} schemas")

    # Create backup if not dry run
    if not dry_run and not backup_dir.exists():
        print(f"Creating backup at {backup_dir}")
        shutil.copytree(examples_dir, backup_dir)

    # Create output directory
    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    # Process each schema
    stats = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "skipped_variables": 0,  # Skipped due to unresolved variables
        "skipped_validation": 0,  # Skipped due to validation failure
        "duplicates_removed": 0,
    }

    for i, (source_file, examples) in enumerate(by_source.items()):
        print(f"\n[{i + 1}/{len(by_source)}] {source_file}")

        # Pick best example to keep (prefer 'technical' style)
        keep = None
        for ex in examples:
            if ex["data"].get("style") == "technical":
                keep = ex
                break
        if not keep:
            keep = examples[0]

        # Count duplicates
        duplicates = [ex for ex in examples if ex["path"] != keep["path"]]
        stats["duplicates_removed"] += len(duplicates)

        print(f"  Keeping: {keep['path'].name}")
        print(f"  Removing: {len(duplicates)} duplicates")

        # Get output_schema
        output_schema = keep["data"].get("output_schema", {})
        if not output_schema:
            print(f"  SKIP: No output_schema")
            stats["failed"] += 1
            continue

        # Check for unresolved variables
        if has_unresolved_variables(output_schema):
            print(f"  SKIP: Contains unresolved variable placeholders")
            stats["skipped_variables"] += 1
            # Still remove duplicates but don't process this one
            if not dry_run:
                for dup in duplicates:
                    dup["path"].unlink()
            continue

        # Generate precise NL
        print(f"  Generating precise NL...")
        precise_nl, concise_nl = generate_precise_nl(output_schema, verbose=verbose)

        if not precise_nl:
            print(f"  ERROR: Failed to generate NL")
            stats["failed"] += 1
            continue

        print(f"  Precise: {precise_nl[:80]}...")
        print(f"  Concise: {concise_nl[:80]}...")

        # Optionally polish with LLM
        natural_nl = None
        if use_llm and client:
            print(f"  Polishing with LLM...")
            natural_nl = polish_with_llm(precise_nl, output_schema, client, model)
            if natural_nl:
                print(f"  Natural: {natural_nl[:80]}...")
            time.sleep(0.3)  # Rate limiting

        # Validate generated content
        intent_to_use = natural_nl if natural_nl else concise_nl
        if not intent_to_use or len(intent_to_use.strip()) < 10:
            # Fall back to precise if concise is empty/too short
            intent_to_use = precise_nl

        # Final validation - make sure we have usable content
        if not precise_nl or len(precise_nl) < 20:
            print(f"  SKIP: Generated NL too short or empty")
            stats["skipped_validation"] += 1
            if not dry_run:
                for dup in duplicates:
                    dup["path"].unlink()
            continue

        # Update and save
        if not dry_run:
            # Create new example with all NL variants
            new_data = {
                "id": keep["data"].get("id", f"ex_{source_file[:8]}"),
                "source_file": source_file,
                "intent_precise": precise_nl,
                "intent_concise": concise_nl if concise_nl else precise_nl[:100],  # Fallback
                "intent": intent_to_use,
                "style": "scientist",
                "output_schema": output_schema,
            }

            # Also regenerate config from output_schema
            config = extract_config(output_schema)
            if config:
                new_data["config"] = config

            # Save to output directory with new name
            new_filename = f"ex_scientist_{keep['data'].get('id', '')[-8:]}.json"
            output_path = output_dir / new_filename
            output_path.write_text(json.dumps(new_data, indent=2))

            # Delete duplicates from original directory
            for dup in duplicates:
                dup["path"].unlink()

            # Delete original (we saved new version)
            keep["path"].unlink()

        stats["processed"] += 1
        stats["success"] += 1

    return stats


def extract_config(output_schema: Dict) -> Optional[Dict]:
    """Extract config from output_schema."""
    config = {}

    # Extract labware
    if output_schema.get("labware"):
        config["labware"] = {}
        seen_labels = set()
        for lw in output_schema["labware"]:
            label = lw.get("label") or f"labware_slot_{lw.get('slot', 'unknown')}"
            if label in seen_labels:
                label = f"{label}_{lw.get('slot', 'x')}"
            seen_labels.add(label)
            config["labware"][label] = {
                "load_name": lw.get("load_name", ""),
                "slot": str(lw.get("slot", ""))
            }

    # Extract pipettes
    if output_schema.get("pipettes"):
        config["pipettes"] = {}
        for i, pip in enumerate(output_schema["pipettes"]):
            mount = pip.get("mount") or ""
            if not mount or mount not in ("left", "right"):
                mount = "left" if i == 0 else "right"
            config["pipettes"][mount] = {
                "model": pip.get("model", ""),
                "tipracks": pip.get("tipracks", [])
            }

    # Extract modules
    if output_schema.get("modules"):
        config["modules"] = {}
        seen_labels = set()
        for mod in output_schema["modules"]:
            label = mod.get("label") or f"module_slot_{mod.get('slot', 'unknown')}"
            if label in seen_labels:
                label = f"{label}_{mod.get('slot', 'x')}"
            seen_labels.add(label)
            config["modules"][label] = {
                "module_type": mod.get("module_type", ""),
                "slot": str(mod.get("slot", ""))
            }

    return config if config.get("labware") or config.get("pipettes") else None


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="Regenerate NL for RAG examples using rule-based conversion"
    )
    parser.add_argument(
        "--input", "-i",
        default=None,
        help="Input examples directory (default: nl2protocol/examples)"
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output directory for regenerated examples (default: nl2protocol/examples_v2)"
    )
    parser.add_argument(
        "--backup", "-b",
        default=None,
        help="Backup directory (default: nl2protocol/examples_backup)"
    )
    parser.add_argument(
        "--use-llm", "-l",
        action="store_true",
        help="Use LLM to polish precise NL into natural language"
    )
    parser.add_argument(
        "--model", "-m",
        default="claude-sonnet-4-20250514",
        help="LLM model to use for polishing"
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=None,
        help="Process only N schemas (for testing)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write files, just show what would happen"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show full tracebacks on errors"
    )
    args = parser.parse_args()

    # Set default paths
    base_dir = Path(__file__).parent.parent.parent
    examples_dir = Path(args.input) if args.input else base_dir / "nl2protocol" / "examples"
    output_dir = Path(args.output) if args.output else base_dir / "nl2protocol" / "examples_v2"
    backup_dir = Path(args.backup) if args.backup else base_dir / "nl2protocol" / "examples_backup"

    print("=" * 60)
    print("RAG Example NL Regeneration")
    print("=" * 60)
    print(f"Input:  {examples_dir}")
    print(f"Output: {output_dir}")
    print(f"Backup: {backup_dir}")
    print(f"Use LLM: {args.use_llm}")
    if args.limit:
        print(f"Limit: {args.limit} schemas")
    if args.dry_run:
        print("DRY RUN - no files will be modified")
    print("=" * 60)

    stats = process_examples(
        examples_dir=examples_dir,
        output_dir=output_dir,
        backup_dir=backup_dir,
        use_llm=args.use_llm,
        model=args.model,
        limit=args.limit,
        dry_run=args.dry_run,
        verbose=args.verbose
    )

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Schemas processed: {stats['processed']}")
    print(f"Successful: {stats['success']}")
    print(f"Failed (no schema): {stats['failed']}")
    print(f"Skipped (unresolved variables): {stats['skipped_variables']}")
    print(f"Skipped (validation): {stats['skipped_validation']}")
    print(f"Duplicates removed: {stats['duplicates_removed']}")

    if args.dry_run:
        print("\n(Dry run - no files were modified)")
    else:
        print(f"\nNew examples saved to: {output_dir}")
        print(f"Original backup at: {backup_dir}")

    return 0 if stats['failed'] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
