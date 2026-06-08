"""
labware_confirm.py — CLI labware-assignment confirmation REPL.

CLI-mode parallel of the browser surface's
`HTMLAssignmentsHandler` / `_confirm_labware_assignments_via_handler`.
Walks the user through `{description: resolved_label}` mappings, lets
them reassign any row by number, and returns the confirmed dict (or
None on quit). Pure presentation + interaction; no spec mutation —
caller applies the result.

Eventually superseded by the ConfirmationStrategy refactor (ADR-0017 #3)
which folds CLI and browser paths into one strategy interface. Until
then this is the standalone CLI implementation.
"""

import sys
from typing import Optional

from . import colors as C


def _log(msg: str = "") -> None:
    """Print to stderr (keeps stdout clean for data output)."""
    print(msg, file=sys.stderr)


def cli_labware_loop(
    spec,
    labware_suggestions: dict,
    config: dict,
    cm,
) -> Optional[dict]:
    """Interactive REPL for `{description → config_label}` confirmation.

    Pre:    `spec` is a `ProtocolSpec` whose `LocationRef.description`s
            drive the displayed rows. `labware_suggestions` is the dict
            from `LabwareResolver.suggest()` keyed on description with
            values exposing `.suggested_label` (None ⇒ unresolved). `config`
            is the loaded lab_config dict (used for `labware[X].load_name`
            display). `cm` is a `ConfirmationManager` whose `.prompt(text)`
            reads one line of input (CLI uses InteractiveCM; tests pass
            ScriptedCM).
    Post:   Displays one row per unique description (deduped in spec
            walk-order) showing the resolver's suggestion or `???`. Loop:
              - Enter (all rows resolved) → returns `{description: label}`,
                warns inline if multiple descriptions map to the same label.
              - Enter (unresolved rows)   → re-prompts; lists unresolved
                descriptions.
              - Digit                     → opens reassignment sub-loop
                listing all available config labels with a `←` marker on
                the current pick.
              - 'q'                       → returns None.
            Returns `{}` when the spec has no LocationRefs at all (nothing
            to confirm).
    Side effects: Writes to stderr via `_log`. Reads stdin via `cm.prompt`.
                  Does not mutate spec or labware_suggestions.
    """
    assignments = []
    seen = set()
    for step in spec.steps:
        for ref in [step.source, step.destination]:
            if ref and ref.description not in seen:
                seen.add(ref.description)
                suggestion = labware_suggestions.get(ref.description)
                suggested = (
                    suggestion.suggested_label
                    if suggestion is not None else None
                )
                assignments.append({
                    "description": ref.description,
                    "resolved_label": suggested,
                })

    if not assignments:
        return {}

    available_labels = list(config.get("labware", {}).keys())
    current = {a["description"]: a["resolved_label"] for a in assignments}

    while True:
        _log(f"\n  {C.label('Labware assignments')}:")
        all_resolved = True
        for i, a in enumerate(assignments, 1):
            desc = a["description"]
            label = current.get(desc)
            if label:
                load_name = config["labware"][label].get("load_name", "")
                _log(f"    [{i}] \"{desc}\" → {C.success(label)} ({C.dim(load_name)})")
            else:
                _log(f"    [{i}] \"{desc}\" → {C.warning('??? (no match)')}")
                all_resolved = False

        if all_resolved:
            response = cm.prompt(
                "Pick a number to change, or Enter to confirm all (q=quit): "
            ).lower()
        else:
            _log(f"  {C.dim('Assign all ??? items before confirming. If your config is missing')}")
            _log(f"  {C.dim('labware, quit (q) and add it to your config.json, then re-run.')}")
            response = cm.prompt("Pick a number to assign (q=quit): ").lower()

        if response == 'q':
            _log("  Aborted.")
            return None
        elif response == '':
            if all_resolved:
                # Warn if multiple descriptions map to the same label
                label_counts: dict = {}
                for desc, label in current.items():
                    label_counts.setdefault(label, []).append(desc)
                for label, descs in label_counts.items():
                    if len(descs) > 1:
                        _log(f"  {C.warning('Note:')} \"{descs[0]}\" and \"{descs[1]}\" "
                             f"both map to {label} — is this the same labware?")
                return current
            else:
                unresolved = [
                    a["description"] for a in assignments
                    if not current.get(a["description"])
                ]
                _log(f"  Cannot confirm — unresolved: {unresolved}")
                continue
        elif response.isdigit():
            idx = int(response) - 1
            if 0 <= idx < len(assignments):
                desc = assignments[idx]["description"]
                _log(f"\n    Reassigning \"{desc}\":")
                for j, label in enumerate(available_labels, 1):
                    load_name = config["labware"][label].get("load_name", "")
                    marker = " ←" if label == current.get(desc) else ""
                    _log(f"      {j}. {label} ({C.dim(load_name)}){marker}")
                pick = cm.prompt(
                    f"Pick label (1-{len(available_labels)}), or Enter to cancel: "
                )
                if pick.isdigit():
                    pick_idx = int(pick) - 1
                    if 0 <= pick_idx < len(available_labels):
                        current[desc] = available_labels[pick_idx]
                    else:
                        _log(f"    Invalid number.")
                elif pick == '':
                    pass  # cancelled
                else:
                    _log(f"    Invalid input.")
            else:
                _log(f"  Invalid number. Pick 1-{len(assignments)}.")
        else:
            _log(f"  Invalid input. Pick a number, Enter, or q.")
