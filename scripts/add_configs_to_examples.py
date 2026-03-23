#!/usr/bin/env python3
"""
Add config field to existing RAG examples.

Extracts config from output_schema (labware, pipettes, modules) and adds
it as a separate 'config' field in our lab_config.json format.

This gives us (NL, config, schema) triples that can be used for:
- ConfigGenerator RAG: (intent, config) pairs
- Protocol generation RAG: (intent, schema) pairs
"""

import json
from pathlib import Path


def extract_config_from_schema(schema: dict) -> dict:
    """
    Convert output_schema to lab_config.json format.

    Input (output_schema):
        {
            "labware": [{"slot": "1", "load_name": "opentrons_96_tiprack_300ul", "label": "tiprack"}],
            "pipettes": [{"mount": "left", "model": "p300_single_gen2", "tipracks": ["tiprack"]}],
            "modules": [{"module_type": "temperature", "slot": "4", "label": "temp_mod"}]
        }

    Output (config):
        {
            "labware": {"tiprack": {"load_name": "opentrons_96_tiprack_300ul", "slot": "1"}},
            "pipettes": {"left": {"model": "p300_single_gen2", "tipracks": ["tiprack"]}},
            "modules": {"temp_mod": {"module_type": "temperature", "slot": "4"}}
        }
    """
    config = {}

    # Extract labware
    if schema.get("labware"):
        config["labware"] = {}
        seen_labels = set()
        for lw in schema["labware"]:
            # Generate label if not present, ensure uniqueness
            label = lw.get("label") or f"labware_slot_{lw.get('slot', 'unknown')}"

            # Handle duplicate labels by appending slot
            if label in seen_labels:
                label = f"{label}_{lw.get('slot', 'x')}"
            seen_labels.add(label)

            config["labware"][label] = {
                "load_name": lw.get("load_name", ""),
                "slot": str(lw.get("slot", ""))
            }

    # Extract pipettes
    if schema.get("pipettes"):
        config["pipettes"] = {}
        for i, pip in enumerate(schema["pipettes"]):
            mount = pip.get("mount") or ""

            # Skip if no mount info at all
            if not mount:
                # Try to assign based on index (first=left, second=right)
                mount = "left" if i == 0 else "right"
            # Normalize mount names (some examples have "p20_mount" instead of "left"/"right")
            elif mount not in ("left", "right"):
                # Try to infer from mount name
                mount_lower = mount.lower()
                if "left" in mount_lower:
                    mount = "left"
                elif "right" in mount_lower:
                    mount = "right"
                # else keep original (will be a non-standard mount name)

            # Avoid overwriting if mount already exists
            if mount in config["pipettes"]:
                mount = f"{mount}_{i}"

            config["pipettes"][mount] = {
                "model": pip.get("model", ""),
                "tipracks": pip.get("tipracks", [])
            }

    # Extract modules (if present)
    if schema.get("modules"):
        config["modules"] = {}
        seen_labels = set()
        for mod in schema["modules"]:
            label = mod.get("label") or f"module_slot_{mod.get('slot', 'unknown')}"

            # Handle duplicate labels
            if label in seen_labels:
                label = f"{label}_{mod.get('slot', 'x')}"
            seen_labels.add(label)

            config["modules"][label] = {
                "module_type": mod.get("module_type", ""),
                "slot": str(mod.get("slot", ""))
            }

    return config


def process_example(file_path: Path) -> tuple[bool, str]:
    """
    Process a single example file, adding config field.

    Returns:
        (success, message)
    """
    try:
        data = json.loads(file_path.read_text())
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}"

    # Skip if already has config
    if "config" in data:
        return True, "already has config"

    # Need output_schema to extract config
    if "output_schema" not in data:
        return False, "missing output_schema"

    schema = data["output_schema"]

    # Extract config
    config = extract_config_from_schema(schema)

    # Validate we got something useful
    if not config.get("labware") and not config.get("pipettes"):
        return False, "could not extract labware or pipettes"

    # Add config to data (insert after intent for readability)
    new_data = {}
    for key, value in data.items():
        new_data[key] = value
        if key == "intent":
            new_data["config"] = config

    # If intent wasn't found, just add at end
    if "config" not in new_data:
        new_data["config"] = config

    # Write back
    file_path.write_text(json.dumps(new_data, indent=2))

    return True, "config added"


def main():
    examples_dir = Path(__file__).parent.parent / "nl2protocol" / "examples"

    if not examples_dir.exists():
        print(f"Examples directory not found: {examples_dir}")
        return 1

    files = list(examples_dir.glob("ex_*.json"))
    print(f"Found {len(files)} example files")

    stats = {
        "success": 0,
        "already_has_config": 0,
        "failed": 0
    }
    failures = []

    for f in files:
        success, msg = process_example(f)
        if success:
            if "already" in msg:
                stats["already_has_config"] += 1
            else:
                stats["success"] += 1
        else:
            stats["failed"] += 1
            failures.append((f.name, msg))

    print(f"\nResults:")
    print(f"  Config added: {stats['success']}")
    print(f"  Already had config: {stats['already_has_config']}")
    print(f"  Failed: {stats['failed']}")

    if failures:
        print(f"\nFailures (first 10):")
        for name, msg in failures[:10]:
            print(f"  {name}: {msg}")

    return 0


if __name__ == "__main__":
    exit(main())
