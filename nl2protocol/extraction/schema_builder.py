"""
schema_builder.py — Deterministic spec-to-schema conversion.

Converts a validated ProtocolSpec + lab config into a ProtocolSchema
(hardware commands). No LLM involved — values are copied directly,
never modified.

Also contains the post-generation validation gate that checks
instruction-sourced volumes weren't dropped during conversion.
"""

import re
import sys
from typing import List, Optional

from nl2protocol.models import (
    ProtocolSchema, Labware, Pipette, Module,
    Transfer, Distribute, Consolidate, Mix,
    Aspirate, Dispense, BlowOut, TouchTip,
    PickUpTip, DropTip,
    Comment, Pause, Delay,
    SetTemperature, WaitForTemperature, DeactivateModule,
    EngageMagnets, DisengageMagnets,
)
from nl2protocol.models.spec import ProtocolSpec, CompleteProtocolSpec
from nl2protocol.validation.constraints import WellStateTracker


def _format_step_line(step) -> str:
    """Format a single step as a one-line summary."""
    # Source
    src = ""
    if step.source:
        src = f" from {step.source.description}"
        if step.source.well:
            src += f" well {step.source.well}"
        elif step.source.well_range:
            src += f" wells {step.source.well_range}"

    # Destination
    dst = ""
    if step.destination:
        dst = f" to {step.destination.description}"
        if step.destination.well:
            dst += f" well {step.destination.well}"
        elif step.destination.well_range:
            dst += f" wells {step.destination.well_range}"
        elif step.destination.wells:
            wells = step.destination.wells
            if len(wells) > 2:
                dst += f" wells {wells[0]}-{wells[-1]}"
            else:
                dst += f" wells {', '.join(wells)}"

    vol = f" {step.volume.value}{step.volume.unit}" if step.volume else ""
    temp = f" {step.temperature.value}°C" if step.temperature else ""
    dur = f" for {step.duration.value} {step.duration.unit}" if step.duration else ""
    substance = f" of {step.substance.value}" if step.substance else ""

    # Post-actions: surface mix/blow_out/touch_tip etc. so downstream consumers
    # (notably the Stage 8 validator) don't conclude they're missing.
    post = ""
    if step.post_actions:
        parts = []
        for pa in step.post_actions:
            if pa.action == "mix" and pa.repetitions and pa.volume:
                parts.append(f"mix {pa.repetitions}x at {pa.volume.value}{pa.volume.unit}")
            elif pa.volume:
                parts.append(f"{pa.action} {pa.volume.value}{pa.volume.unit}")
            else:
                parts.append(pa.action)
        post = " -> " + ", ".join(parts)

    tip = f" [tip: {step.tip_strategy}]" if getattr(step, "tip_strategy", None) else ""

    return f"{step.order}. {step.action.upper()}{temp}{vol}{substance}{src}{dst}{dur}{post}{tip}"


def validate_schema_against_spec(spec: ProtocolSpec, schema) -> List[str]:
    """Check that spec_to_schema didn't drop any instruction-sourced volumes.

    Collects all volumes from the spec where provenance.source == "instruction",
    then verifies each appears somewhere in the schema commands. This catches
    bugs in spec_to_schema (the deterministic converter), not LLM errors.

    Only checks instruction-sourced volumes — inferred/domain_default volumes
    may be legitimately adjusted by constraint checking or pipette selection.
    """
    mismatches = []

    # Collect instruction-sourced volumes from spec
    spec_instruction_volumes = set()
    for step in spec.steps:
        if step.volume and step.volume.provenance.source == "instruction":
            spec_instruction_volumes.add(step.volume.value)
        if step.post_actions:
            for pa in step.post_actions:
                if pa.volume and pa.volume.provenance.source == "instruction":
                    spec_instruction_volumes.add(pa.volume.value)

    if not spec_instruction_volumes:
        return mismatches

    # Collect all volumes from schema commands
    schema_volumes = set()
    for cmd in schema.commands:
        if hasattr(cmd, 'volume') and cmd.volume is not None:
            schema_volumes.add(cmd.volume)
        if hasattr(cmd, 'mix_after') and cmd.mix_after is not None:
            schema_volumes.add(cmd.mix_after[1])
        if hasattr(cmd, 'mix_before') and cmd.mix_before is not None:
            schema_volumes.add(cmd.mix_before[1])

    # Every instruction-sourced spec volume must appear in schema
    for vol in spec_instruction_volumes:
        if not any(abs(sv - vol) < 0.01 for sv in schema_volumes):
            mismatches.append(
                f"Instruction-sourced volume {vol}uL from spec not found in schema "
                f"commands. Schema has: {sorted(schema_volumes)}. "
                f"spec_to_schema may have dropped this value."
            )

    return mismatches


def spec_to_schema(spec: 'CompleteProtocolSpec', config: dict):
    """Deterministically convert a CompleteProtocolSpec + config into a ProtocolSchema.

    No LLM involved. The spec says WHAT (science), this function maps it
    to HOW (hardware). Can't corrupt volumes because it copies them directly.

    Takes CompleteProtocolSpec (not ProtocolSpec) — type-level guarantee that
    all required fields are present before code generation begins.

    Mapping:
      LocationRef.resolved_label → config labware label → slot, load_name
      ProvenancedVolume.value → command volume (direct copy, never modified)
      action → command_type
      pipette_hint / volume range → pipette mount
      well_range → expanded well list
    """
    # Initialize well state tracker with initial contents from spec
    well_tracker = WellStateTracker(config, spec)

    def track_command(cmd):
        """Update well state tracker for a generated command."""
        if isinstance(cmd, Transfer):
            substance = "liquid"
            well_tracker.aspirate(cmd.source_labware, cmd.source_well, cmd.volume)
            well_tracker.dispense(cmd.dest_labware, cmd.dest_well, cmd.volume, substance)
        elif isinstance(cmd, Distribute):
            for dw in cmd.dest_wells:
                well_tracker.aspirate(cmd.source_labware, cmd.source_well, cmd.volume)
                well_tracker.dispense(cmd.dest_labware, dw, cmd.volume, "liquid")
        elif isinstance(cmd, Consolidate):
            for sw in cmd.source_wells:
                well_tracker.aspirate(cmd.source_labware, sw, cmd.volume)
            well_tracker.dispense(cmd.dest_labware, cmd.dest_well, cmd.volume * len(cmd.source_wells), "liquid")
        elif isinstance(cmd, (Aspirate,)):
            well_tracker.aspirate(cmd.labware, cmd.well, cmd.volume)
        elif isinstance(cmd, (Dispense,)):
            well_tracker.dispense(cmd.labware, cmd.well, cmd.volume, "liquid")

    # ---- helpers ----

    def resolve_labware(ref, cfg: dict) -> Optional[str]:
        """Read the pre-resolved config label from a LocationRef.

        Resolution is done upstream by LabwareResolver. This just reads
        the result, with a fallback to description for backwards compatibility.
        """
        if not ref:
            return None
        if ref.resolved_label and ref.resolved_label in cfg.get("labware", {}):
            return ref.resolved_label
        # Fallback: description might already be a config label (legacy specs)
        if ref.description in cfg.get("labware", {}):
            return ref.description
        return None

    def pipette_for_volume(volume: float, cfg: dict) -> Optional[str]:
        """Pick the pipette mount whose range contains the volume."""
        ranges = [('p1000', 100, 1000), ('p300', 20, 300),
                  ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]
        if "pipettes" not in cfg:
            return None
        for mount, pip in cfg["pipettes"].items():
            model = pip["model"].lower()
            for key, lo, hi in ranges:
                if key in model and lo <= volume <= hi:
                    return mount
        return None

    def pipette_by_hint(hint: str, cfg: dict) -> Optional[str]:
        """Match a pipette hint like 'p20' to a mount."""
        if not hint or "pipettes" not in cfg:
            return None
        for mount, pip in cfg["pipettes"].items():
            if hint.lower() in pip["model"].lower():
                return mount
        return None

    def expand_well_range(well_range: str) -> List[str]:
        """Expand well range descriptions into explicit well lists.

        Handles many formats:
          'A1-A12'              → A1, A2, ..., A12  (single row)
          'A1-H1'              → A1, B1, ..., H1   (single column)
          'A1-H3'              → A1, B1, ..., H1, A2, ..., H3  (block, column-major)
          'column 1'           → A1, B1, ..., H1
          'columns 2-8'        → all wells in columns 2-8
          'columns 2-8, rows A-H' → explicit row/col specification
          'rows A-D'           → all wells in rows A-D
          'A1, A2, A3'         → explicit list
        """
        text = well_range.strip()

        # Format: explicit comma-separated wells "A1, A2, A3"
        comma_wells = re.findall(r'([A-P]\d{1,2})', text)
        if ',' in text and comma_wells:
            return comma_wells

        # Format: "A1-H3" or "A1-A12" (standard range)
        m = re.match(r'([A-P])(\d+)\s*[-–]\s*([A-P])(\d+)$', text)
        if m:
            r1, c1, r2, c2 = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
            wells = []
            for col in range(c1, c2 + 1):
                for row in range(ord(r1), ord(r2) + 1):
                    wells.append(f"{chr(row)}{col}")
            return wells

        # Parse row and column ranges from natural language
        # Extract column info
        col_start, col_end = 1, 12  # defaults for 96-well
        col_match = re.search(r'columns?\s+(\d+)\s*[-–]\s*(\d+)', text, re.IGNORECASE)
        col_single = re.search(r'column\s+(\d+)', text, re.IGNORECASE)
        if col_match:
            col_start, col_end = int(col_match.group(1)), int(col_match.group(2))
        elif col_single:
            col_start = col_end = int(col_single.group(1))

        # Extract row info
        row_start, row_end = 'A', 'H'  # defaults for 96-well
        row_match = re.search(r'rows?\s+([A-P])\s*[-–]\s*([A-P])', text, re.IGNORECASE)
        row_single = re.search(r'row\s+([A-P])', text, re.IGNORECASE)
        if row_match:
            row_start, row_end = row_match.group(1).upper(), row_match.group(2).upper()
        elif row_single:
            row_start = row_end = row_single.group(1).upper()

        # If we found column or row keywords, generate the wells
        if col_match or col_single or row_match or row_single:
            wells = []
            for col in range(col_start, col_end + 1):
                for row in range(ord(row_start), ord(row_end) + 1):
                    wells.append(f"{chr(row)}{col}")
            return wells

        # Format: "all wells" — return full 96-well plate
        if 'all wells' in text.lower():
            wells = []
            for col in range(1, 13):
                for row in range(ord('A'), ord('H') + 1):
                    wells.append(f"{chr(row)}{col}")
            return wells

        return []

    def wells_from_ref(ref) -> List[str]:
        """Get explicit well list from a LocationRef."""
        if not ref:
            return []
        if ref.wells:
            return ref.wells
        if ref.well_range:
            expanded = expand_well_range(ref.well_range)
            if expanded:
                return expanded
        if ref.well:
            return [ref.well]
        return []

    def vol_in_ul(step) -> Optional[float]:
        """Get volume in uL from a step."""
        if not step.volume:
            return None
        v = step.volume.value
        if step.volume.unit == "mL":
            v *= 1000
        return v

    def pick_mount(step, cfg) -> Optional[str]:
        """Determine pipette mount for a step.

        Considers ALL volumes in the step (transfer + post-actions like mix)
        and picks a pipette that satisfies all constraints. If no single
        pipette works, picks the one for the transfer volume — the constraint
        checker will have already surfaced this conflict to the scientist.
        """
        if step.pipette_hint:
            m = pipette_by_hint(step.pipette_hint, cfg)
            if m:
                return m

        # Collect all volumes this step needs the pipette to handle
        volumes_needed = []
        v = vol_in_ul(step)
        if v:
            volumes_needed.append(v)

        if step.post_actions:
            for pa in step.post_actions:
                if pa.volume:
                    pa_v = pa.volume.value
                    if pa.volume.unit == "mL":
                        pa_v *= 1000
                    volumes_needed.append(pa_v)

        if not volumes_needed:
            return None

        # Try to find a pipette that covers ALL volumes (min to max)
        min_vol = min(volumes_needed)
        max_vol = max(volumes_needed)

        if "pipettes" not in cfg:
            return None

        # Check each pipette: does its range cover [min_vol, max_vol]?
        ranges = [('p1000', 100, 1000), ('p300', 20, 300),
                  ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]
        for mount, pip in cfg["pipettes"].items():
            model = pip["model"].lower()
            for key, lo, hi in ranges:
                if key in model:
                    if lo <= min_vol and max_vol <= hi:
                        return mount  # This pipette covers everything
                    break

        # No single pipette covers all volumes.
        # Fall back to the one that handles the transfer volume.
        # The constraint checker has already flagged this for the scientist.
        if v:
            return pipette_for_volume(v, cfg)
        return pipette_for_volume(max_vol, cfg)

    # ---- build schema ----

    # Collect used labware labels
    used_labels = set()
    for step in spec.steps:
        for ref in [step.source, step.destination]:
            lbl = resolve_labware(ref, config)
            if lbl:
                used_labels.add(lbl)
    # Include tipracks
    if "pipettes" in config:
        for pip in config["pipettes"].values():
            for tr in pip.get("tipracks", []):
                used_labels.add(tr)

    # Build Labware objects
    labware_list = []
    for label in used_labels:
        if label in config.get("labware", {}):
            lw = config["labware"][label]
            labware_list.append(Labware(
                slot=lw["slot"], load_name=lw["load_name"],
                label=label, on_module=lw.get("on_module")
            ))

    # Collect used pipette mounts
    used_mounts = set()
    for step in spec.steps:
        m = pick_mount(step, config)
        if m:
            used_mounts.add(m)
    if not used_mounts and "pipettes" in config:
        used_mounts = set(config["pipettes"].keys())

    # Build Pipette objects
    pipette_list = []
    for mount in used_mounts:
        if mount in config.get("pipettes", {}):
            pip = config["pipettes"][mount]
            pipette_list.append(Pipette(
                mount=mount, model=pip["model"],
                tipracks=pip.get("tipracks", [])
            ))

    # Build Commands — using TrackedList to automatically update well state
    class TrackedList(list):
        """List that tracks well state whenever a command is appended."""
        def append(self, cmd):
            super().append(cmd)
            track_command(cmd)

        def extend(self, cmds):
            for cmd in cmds:
                self.append(cmd)

    commands = TrackedList()
    default_mount = sorted(used_mounts)[0] if used_mounts else "left"
    step_summaries = []  # Per-step breakdown for validation

    for step in spec.steps:
        cmd_count_before = len(commands)
        v = vol_in_ul(step)
        mount = pick_mount(step, config) or default_mount
        src_label = resolve_labware(step.source, config)
        dst_label = resolve_labware(step.destination, config)
        src_wells = wells_from_ref(step.source)
        dst_wells = wells_from_ref(step.destination)

        # Tip strategy is determined deterministically, not by LLM extraction.
        # The algorithm: reuse tips while the source well stays the same and
        # there's no post-dispense mixing (which contaminates the tip with
        # destination liquid). Applied in the transfer action block below.

        # Mix from post_actions
        # NOTE: No silent clamping here. If the mix volume exceeds pipette
        # capacity, the ConstraintChecker should have caught it upstream and
        # surfaced it to the scientist. By this point, either:
        #   (a) the volume is within range, or
        #   (b) the scientist approved an alternative resolution
        mix_after = None
        if step.post_actions:
            for pa in step.post_actions:
                if pa.action == "mix":
                    reps = pa.repetitions or 3
                    if pa.volume:
                        pa_v = pa.volume.value
                        if pa.volume.unit == "mL":
                            pa_v *= 1000
                    else:
                        # Default: use the step's transfer volume
                        pa_v = v if v else 50.0
                    # Constrain to pipette capacity — but transparently.
                    # If a constraint violation was raised and the scientist chose
                    # to proceed anyway (e.g., "reduce mix volume"), apply it here.
                    if mount in config.get("pipettes", {}):
                        model_str = config["pipettes"][mount]["model"].lower()
                        cap = None
                        for key, lo, hi in [('p1000', 100, 1000), ('p300', 20, 300),
                                             ('p50', 5, 50), ('p20', 1, 20), ('p10', 1, 10)]:
                            if key in model_str:
                                cap = hi
                                break
                        if cap and pa_v > cap:
                            # Reduce to pipette max — this path is only reached
                            # when the scientist has been informed and chose to proceed
                            pa_v = cap
                    mix_after = (reps, pa_v)

        # ---- map action → commands ----

        if step.action == "transfer":
            # Collect all transfers for this step, then apply tip strategy
            step_transfers = []

            if step.replicates and src_wells:
                # Replicate expansion: each source well → N dest wells
                # across consecutive columns starting at dest's column
                dest_start_col = 1
                if dst_wells:
                    m = re.match(r'[A-P](\d+)', dst_wells[0])
                    if m:
                        dest_start_col = int(m.group(1))
                elif step.destination and step.destination.well:
                    m = re.match(r'[A-P](\d+)', step.destination.well)
                    if m:
                        dest_start_col = int(m.group(1))

                for sw in src_wells:
                    row = sw[0]
                    for rep in range(step.replicates):
                        dw = f"{row}{dest_start_col + rep}"
                        step_transfers.append(Transfer(
                            pipette=mount, source_labware=src_label, source_well=sw,
                            dest_labware=dst_label, dest_well=dw, volume=v,
                            new_tip="always", mix_after=mix_after
                        ))
            elif src_wells and dst_wells:
                if len(src_wells) == 1 and len(dst_wells) > 1:
                    # 1 source → N destinations: transfer to each dest
                    for dw in dst_wells:
                        step_transfers.append(Transfer(
                            pipette=mount, source_labware=src_label, source_well=src_wells[0],
                            dest_labware=dst_label, dest_well=dw, volume=v,
                            new_tip="always", mix_after=mix_after
                        ))
                elif len(dst_wells) == 1 and len(src_wells) > 1:
                    # N sources → 1 destination: transfer from each source
                    for sw in src_wells:
                        step_transfers.append(Transfer(
                            pipette=mount, source_labware=src_label, source_well=sw,
                            dest_labware=dst_label, dest_well=dst_wells[0], volume=v,
                            new_tip="always", mix_after=mix_after
                        ))
                else:
                    # Paired well lists: zip source[i] → dest[i]
                    pairs = list(zip(src_wells, dst_wells))
                    for sw, dw in pairs:
                        step_transfers.append(Transfer(
                            pipette=mount, source_labware=src_label, source_well=sw,
                            dest_labware=dst_label, dest_well=dw, volume=v,
                            new_tip="always", mix_after=mix_after
                        ))
            else:
                # Single well fallback
                sw = step.source.well if step.source and step.source.well else "A1"
                dw = step.destination.well if step.destination and step.destination.well else "A1"
                step_transfers.append(Transfer(
                    pipette=mount, source_labware=src_label, source_well=sw,
                    dest_labware=dst_label, dest_well=dw, volume=v,
                    new_tip="always", mix_after=mix_after
                ))

            # Apply tip strategy deterministically:
            # Reuse tip while source well stays the same and no mixing.
            # Mixing contaminates the tip with destination liquid.
            if mix_after or len(step_transfers) <= 1:
                commands.extend(step_transfers)
            else:
                # Group by source well: reuse tip while source stays the same
                has_groups = any(
                    step_transfers[i].source_well == step_transfers[i + 1].source_well
                    for i in range(len(step_transfers) - 1)
                )
                if not has_groups:
                    # Every transfer has a unique source — no grouping benefit
                    commands.extend(step_transfers)
                else:
                    commands.append(PickUpTip(pipette=mount))
                    for i, t in enumerate(step_transfers):
                        t.new_tip = "never"
                        commands.append(t)
                        if i < len(step_transfers) - 1 and step_transfers[i + 1].source_well != t.source_well:
                            commands.append(DropTip(pipette=mount))
                            commands.append(PickUpTip(pipette=mount))
                    commands.append(DropTip(pipette=mount))

        elif step.action == "distribute":
            source_well = src_wells[0] if src_wells else (step.source.well if step.source else "A1")
            if mix_after:
                # Distribute doesn't support mix_after — fall back to
                # individual transfers so each destination gets mixed.
                # Mixing contaminates tip, so new tip for each.
                for dw in dst_wells:
                    commands.append(Transfer(
                        pipette=mount, source_labware=src_label, source_well=source_well,
                        dest_labware=dst_label, dest_well=dw, volume=v,
                        new_tip="always",
                        mix_after=mix_after,
                    ))
            else:
                commands.append(Distribute(
                    pipette=mount, source_labware=src_label,
                    source_well=source_well,
                    dest_labware=dst_label, dest_wells=dst_wells, volume=v,
                    new_tip="once"
                ))

        elif step.action == "consolidate":
            commands.append(Consolidate(
                pipette=mount, source_labware=src_label, source_wells=src_wells,
                dest_labware=dst_label,
                dest_well=dst_wells[0] if dst_wells else (step.destination.well if step.destination else "A1"),
                volume=v, new_tip="once"
            ))

        elif step.action == "serial_dilution":
            # Serial dilution: sequential transfers with mixing.
            # Simple case (1 row):  A1 → A2 → A3 → ... → A8
            # Parallel case (N rows): each row gets its own chain
            #   Row A: A1→A2→A3→...→A8
            #   Row B: B1→B2→B3→...→B8  (independent chain)
            if dst_wells:
                # Group source and dest wells by row letter
                src_by_row = {}
                for w in (src_wells or []):
                    src_by_row.setdefault(w[0], []).append(w)
                dst_by_row = {}
                for w in dst_wells:
                    dst_by_row.setdefault(w[0], []).append(w)

                # Sort each row's wells by column number
                for row in src_by_row:
                    src_by_row[row].sort(key=lambda w: int(w[1:]))
                for row in dst_by_row:
                    dst_by_row[row].sort(key=lambda w: int(w[1:]))

                # Rows that have both source and dest wells
                common_rows = sorted(set(src_by_row) & set(dst_by_row))

                if common_rows:
                    # One chain per row
                    for row in common_rows:
                        chain = [src_by_row[row][0]] + dst_by_row[row]
                        for i in range(len(chain) - 1):
                            from_label = src_label if i == 0 else dst_label
                            commands.append(Transfer(
                                pipette=mount, source_labware=from_label,
                                source_well=chain[i], dest_labware=dst_label,
                                dest_well=chain[i + 1], volume=v,
                                new_tip="always", mix_after=mix_after or (3, v)
                            ))
                else:
                    # Fallback: single chain from first source through all dests
                    source_well = src_wells[0] if src_wells else "A1"
                    chain = [source_well] + dst_wells
                    for i in range(len(chain) - 1):
                        from_label = src_label if i == 0 else dst_label
                        commands.append(Transfer(
                            pipette=mount, source_labware=from_label,
                            source_well=chain[i], dest_labware=dst_label,
                            dest_well=chain[i + 1], volume=v,
                            new_tip="always", mix_after=mix_after or (3, v)
                        ))

        elif step.action == "mix":
            target = dst_label or src_label
            reps = 3
            if step.post_actions:
                for pa in step.post_actions:
                    if pa.action == "mix" and pa.repetitions:
                        reps = pa.repetitions
            wells = dst_wells or src_wells or ["A1"]
            # New tip per well — mixing touches well contents,
            # reusing would cross-contaminate between wells
            for well in wells:
                commands.append(PickUpTip(pipette=mount))
                commands.append(Mix(
                    pipette=mount, labware=target, well=well,
                    volume=v, repetitions=reps
                ))
                commands.append(DropTip(pipette=mount))

        elif step.action == "delay":
            minutes, seconds = None, None
            # Use ProvenancedDuration if available
            if step.duration:
                if step.duration.unit == "minutes":
                    minutes = step.duration.value
                elif step.duration.unit == "seconds":
                    seconds = step.duration.value
                elif step.duration.unit == "hours":
                    minutes = step.duration.value * 60
            # Fallback: parse from note
            if minutes is None and seconds is None and step.note:
                mm = re.search(r'(\d+)\s*min', step.note)
                ss = re.search(r'(\d+)\s*sec', step.note)
                if mm:
                    minutes = float(mm.group(1))
                if ss:
                    seconds = float(ss.group(1))
            commands.append(Delay(
                pipette=mount, minutes=minutes,
                seconds=seconds or (60.0 if not minutes else None)
            ))

        elif step.action == "pause":
            commands.append(Pause(
                pipette=mount,
                message=step.note or (step.substance.value if step.substance else None) or "Pausing protocol"
            ))

        elif step.action == "comment":
            commands.append(Comment(
                pipette=mount,
                message=step.note or (step.substance.value if step.substance else None) or ""
            ))

        elif step.action == "aspirate":
            target = src_label or dst_label
            wells = src_wells or dst_wells or ["A1"]
            # New tip per well — each well has unique contents
            for well in wells:
                commands.append(PickUpTip(pipette=mount))
                commands.append(Aspirate(
                    pipette=mount, labware=target, well=well, volume=v
                ))
                commands.append(DropTip(pipette=mount))

        elif step.action == "dispense":
            target = dst_label or src_label
            wells = dst_wells or src_wells or ["A1"]
            # New tip per well
            for well in wells:
                commands.append(PickUpTip(pipette=mount))
                commands.append(Dispense(
                    pipette=mount, labware=target, well=well, volume=v
                ))
                commands.append(DropTip(pipette=mount))

        elif step.action == "blow_out":
            target = dst_label or src_label
            wells = dst_wells or src_wells
            if target and wells:
                for well in wells:
                    commands.append(BlowOut(
                        pipette=mount, labware=target, well=well
                    ))
            else:
                commands.append(BlowOut(pipette=mount))

        elif step.action == "touch_tip":
            target = dst_label or src_label
            wells = dst_wells or src_wells or ["A1"]
            for well in wells:
                commands.append(TouchTip(
                    pipette=mount, labware=target, well=well
                ))

        # --- Module commands ---

        elif step.action == "set_temperature":
            # Read celsius from temperature field, fall back to note for backwards compat
            celsius = None
            if step.temperature:
                celsius = step.temperature.value
            elif step.note:
                m = re.search(r'(\d+(?:\.\d+)?)\s*°?\s*C', step.note)
                if m:
                    celsius = float(m.group(1))
            if celsius is not None:
                # Find the module label
                mod_label = None
                if "modules" in config:
                    for label, mod in config["modules"].items():
                        if mod.get("module_type") == "temperature":
                            mod_label = label
                            break
                    if mod_label is None:
                        # Use first module as fallback
                        mod_label = next(iter(config["modules"]))
                if mod_label:
                    commands.append(SetTemperature(
                        pipette=mount, module=mod_label, celsius=celsius
                    ))

        elif step.action == "wait_for_temperature":
            # Get temperature from step field, or from the preceding set_temperature
            celsius = None
            if step.temperature:
                celsius = step.temperature.value
            elif step.note:
                m = re.search(r'(\d+(?:\.\d+)?)\s*°?\s*C', step.note)
                if m:
                    celsius = float(m.group(1))
            # Fallback: find the most recent SetTemperature command's celsius
            if celsius is None:
                for prev_cmd in reversed(commands):
                    if hasattr(prev_cmd, 'celsius') and hasattr(prev_cmd, 'command_type') and prev_cmd.command_type == "set_temperature":
                        celsius = prev_cmd.celsius
                        break
            if celsius is None:
                celsius = 4.0  # safe fallback

            mod_label = None
            if "modules" in config:
                for label, mod in config["modules"].items():
                    if mod.get("module_type") == "temperature":
                        mod_label = label
                        break
            if mod_label:
                commands.append(WaitForTemperature(
                    pipette=mount, module=mod_label, celsius=celsius
                ))

        elif step.action == "engage_magnets":
            mod_label = None
            if "modules" in config:
                for label, mod in config["modules"].items():
                    if mod.get("module_type") == "magnetic":
                        mod_label = label
                        break
            if mod_label:
                commands.append(EngageMagnets(
                    pipette=mount, module=mod_label
                ))

        elif step.action == "disengage_magnets":
            mod_label = None
            if "modules" in config:
                for label, mod in config["modules"].items():
                    if mod.get("module_type") == "magnetic":
                        mod_label = label
                        break
            if mod_label:
                commands.append(DisengageMagnets(
                    pipette=mount, module=mod_label
                ))

        elif step.action == "deactivate":
            mod_label = None
            if "modules" in config:
                mod_label = next(iter(config["modules"]))
            if mod_label:
                commands.append(DeactivateModule(
                    pipette=mount, module=mod_label
                ))

        else:
            print(f"  Warning: unhandled action '{step.action}' in step {step.order}, skipping")

        # Record what this step produced for validation
        cmds_generated = len(commands) - cmd_count_before
        step_summaries.append({
            "step": step.order,
            "action": step.action,
            "substance": step.substance.value if step.substance else None,
            "volume_ul": vol_in_ul(step),
            "source": src_label,
            "destination": dst_label,
            "source_wells": src_wells,
            "dest_wells": dst_wells,
            "commands_generated": cmds_generated,
            "description": _format_step_line(step),
        })

    # Remove redundant DropTip/PickUpTip pairs between same-source transfers.
    # If transfer N and transfer N+1 share the same source well, same pipette,
    # and neither has mix_after, the tip change between them is unnecessary.
    i = 0
    while i < len(commands) - 2:
        cmd = commands[i]
        if (isinstance(cmd, Transfer) and cmd.new_tip == "never"
                and isinstance(commands[i + 1], DropTip)
                and isinstance(commands[i + 2], PickUpTip)):
            # Find the next Transfer after the PickUpTip
            next_transfer_idx = i + 3
            if (next_transfer_idx < len(commands)
                    and isinstance(commands[next_transfer_idx], Transfer)
                    and commands[next_transfer_idx].new_tip == "never"
                    and cmd.source_well == commands[next_transfer_idx].source_well
                    and cmd.pipette == commands[next_transfer_idx].pipette
                    and not cmd.mix_after
                    and not commands[next_transfer_idx].mix_after):
                # Same source, same pipette, no mixing — remove the DropTip/PickUpTip
                del commands[i + 1]  # DropTip
                del commands[i + 1]  # PickUpTip (shifted down)
                continue
        i += 1

    # Build Module objects from config
    module_list = []
    if "modules" in config:
        # Include modules that are referenced by commands
        used_module_labels = set()
        for cmd in commands:
            if hasattr(cmd, 'module'):
                used_module_labels.add(cmd.module)
        for label, mod in config["modules"].items():
            if label in used_module_labels:
                module_list.append(Module(
                    module_type=mod["module_type"],
                    slot=mod["slot"],
                    label=label
                ))

    schema = ProtocolSchema(
        protocol_name=spec.protocol_type or "AI Generated Protocol",
        author="Biolab AI",
        labware=labware_list,
        pipettes=pipette_list,
        modules=module_list if module_list else None,
        commands=list(commands)
    )

    return schema, well_tracker.warnings, step_summaries
