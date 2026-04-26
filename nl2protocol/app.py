import io
import sys
import json
import os
import logging
import warnings
from contextlib import redirect_stderr
from dataclasses import dataclass
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv

from .models import ProtocolSchema
from .parser import ProtocolParser

# Suppress opentrons simulator warnings
logging.getLogger("opentrons").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", module="opentrons")


LINE_WIDTH = 60  # Same as the ===== separator

from . import colors as C

def _log(msg: str = ""):
    """Print progress/status to stderr (keeps stdout clean for data output)."""
    print(msg, file=sys.stderr)


def _wrap(text: str, indent: str = "  ", width: int = LINE_WIDTH) -> str:
    """Wrap text to fit within LINE_WIDTH, preserving the indent."""
    import textwrap
    return textwrap.fill(text, width=width, initial_indent=indent,
                         subsequent_indent=indent)


def _stage(label: str):
    """Print a stage header with visual separation."""
    _log()
    _log(C.separator(LINE_WIDTH))
    _log(C.header(label))


def _prompt_input(msg: str) -> str:
    """Styled input prompt that stands out from surrounding output."""
    _log()  # breathing room before prompt
    return input(C.prompt(msg)).strip()

from opentrons import simulate


@dataclass
class VerificationResult:
    """Result from LLM verification of generated protocol."""
    matches: bool
    confidence: float
    issues: list
    summary: str

    def __str__(self) -> str:
        status = "MATCH" if self.matches else "MISMATCH"
        result = f"[{status}] Confidence: {self.confidence:.0%}"
        if self.issues:
            result += f"\nIssues: {', '.join(self.issues)}"
        result += f"\nSummary: {self.summary}"
        return result


VERIFICATION_PROMPT = """You are verifying if a generated Opentrons protocol matches the user's intent.

ORIGINAL INTENT:
{intent}

GENERATED PROTOCOL SCHEMA:
- Protocol Name: {protocol_name}
- Labware: {labware}
- Pipettes: {pipettes}
- Modules: {modules}
- Number of Commands: {num_commands}
- Command Summary: {command_summary}

LABWARE MAPPING (how user's wording was resolved to physical labware):
{labware_mapping}
The user's informal names (e.g. "tube rack") may map to different Opentrons load names
(e.g. a deep well plate). This is expected — focus on whether the protocol actions are correct,
not whether load names match the user's wording.

Questions:
1. Does this protocol achieve what the user asked for?
2. Are there any obvious mismatches between intent and actions?
3. Summarize what this protocol actually does in one sentence.

Respond with JSON only:
{{"matches": true/false, "confidence": 0.0-1.0, "issues": ["issue1", ...], "summary": "..."}}
"""


def verify_intent_match(intent: str, schema: ProtocolSchema, labware_mapping: dict = None) -> VerificationResult:
    """
    Ask Claude to verify the generated protocol matches the original intent.

    Args:
        intent: Original natural language instruction
        schema: Generated protocol schema

    Returns:
        VerificationResult with match status, confidence, issues, and summary
    """
    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Skip verification if no API key
        return VerificationResult(
            matches=True,
            confidence=1.0,
            issues=[],
            summary="Verification skipped (no API key)"
        )

    client = Anthropic(api_key=api_key)

    # Build command summary
    cmd_counts = {}
    for cmd in schema.commands:
        cmd_type = cmd.command_type
        cmd_counts[cmd_type] = cmd_counts.get(cmd_type, 0) + 1
    command_summary = ", ".join([f"{count}x {cmd_type}" for cmd_type, count in cmd_counts.items()])

    # Build labware summary
    labware_summary = ", ".join([f"{lw.load_name} in slot {lw.slot}" for lw in schema.labware])

    # Build pipette summary
    pipette_summary = ", ".join([f"{p.model} on {p.mount}" for p in schema.pipettes])

    # Build module summary
    module_summary = "None"
    if schema.modules:
        module_summary = ", ".join([f"{m.module_type} in slot {m.slot}" for m in schema.modules])

    # Format labware mapping: "tube rack" → sample_rack (usascientific_96_wellplate_2.4ml_deep)
    if labware_mapping:
        mapping_lines = [
            f'  "{desc}" → {info["config_label"]} ({info["load_name"]})'
            for desc, info in labware_mapping.items()
        ]
        mapping_summary = "\n".join(mapping_lines)
    else:
        mapping_summary = "  (not available)"

    prompt = VERIFICATION_PROMPT.format(
        intent=intent,
        protocol_name=schema.protocol_name,
        labware=labware_summary,
        pipettes=pipette_summary,
        modules=module_summary,
        num_commands=len(schema.commands),
        command_summary=command_summary,
        labware_mapping=mapping_summary
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}]
        )

        result_text = response.content[0].text.strip()

        # Parse JSON response
        if "```json" in result_text:
            result_text = result_text.split("```json")[1].split("```")[0]
        elif "```" in result_text:
            result_text = result_text.split("```")[1].split("```")[0]

        result = json.loads(result_text.strip())

        return VerificationResult(
            matches=result.get("matches", True),
            confidence=result.get("confidence", 1.0),
            issues=result.get("issues", []),
            summary=result.get("summary", "")
        )

    except Exception as e:
        # On error, assume match to avoid blocking valid protocols
        _log(f"  Verification skipped ({type(e).__name__})")
        return VerificationResult(
            matches=True,
            confidence=0.5,
            issues=["Verification failed due to error"],
            summary="Could not verify protocol"
        )


@dataclass
class PipelineResult:
    """Result from a successful protocol generation pipeline run."""
    script: str
    simulation_log: str
    protocol_schema: ProtocolSchema
    runlog: list
    config: dict


def generate_python_script(protocol: ProtocolSchema) -> str:
    """Generates a valid Opentrons Python script from the Pydantic schema.
    the pydantic schema is generated by the llm, given the natural language.
    this converts nl to script
    """

    lines = [
        "from opentrons import protocol_api",
        "",
        f"metadata = {{'protocolName': '{protocol.protocol_name}', 'author': '{protocol.author}', 'apiLevel': '2.15'}}",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
    ]

    # Protocol has objects and actions: labware, pipettes, modules, and commands
    # Order matters: Modules first, then labware (some may load onto modules), then pipettes

    # Load Modules first (labware may need to load onto them)
    module_map = {}
    if protocol.modules:
        for mod in protocol.modules:
            var_name = f"mod_{mod.slot.replace('-', '_')}"
            module_type_map = {
                "temperature": "temperature module gen2",
                "magnetic": "magnetic module gen2",
                "heater_shaker": "heaterShakerModuleV1",
                "thermocycler": "thermocyclerModuleV2",
            }
            api_name = module_type_map.get(mod.module_type, mod.module_type)
            lines.append(f"    {var_name} = protocol.load_module('{api_name}', '{mod.slot}')")
            module_map[mod.slot] = var_name
            if mod.label:
                module_map[mod.label] = var_name
        lines.append("")

    # Load Labware (either directly on deck or onto modules)
    labware_map = {}
    for lw in protocol.labware:
        var_name = f"lw_{lw.slot}"
        label_arg = f", label='{lw.label}'" if lw.label else ""

        if lw.on_module:
            # Load labware onto a module
            mod_var = module_map.get(lw.on_module)
            if not mod_var:
                raise ValueError(f"Module '{lw.on_module}' not found for labware '{lw.label or lw.slot}'")
            lines.append(f"    {var_name} = {mod_var}.load_labware('{lw.load_name}'{label_arg})")
        else:
            # Load labware directly on deck slot
            lines.append(f"    {var_name} = protocol.load_labware('{lw.load_name}', '{lw.slot}'{label_arg})")

        labware_map[lw.slot] = var_name
        if lw.label:
            labware_map[lw.label] = var_name
    lines.append("")

    # Load Pipettes
    for pip in protocol.pipettes:
        tiprack_vars = [labware_map.get(tr) for tr in pip.tipracks if labware_map.get(tr)]
        tiprack_str = f", tip_racks=[{', '.join(tiprack_vars)}]" if tiprack_vars else ""
        lines.append(f"    pip_{pip.mount} = protocol.load_instrument('{pip.model}', '{pip.mount}'{tiprack_str})")
    lines.append("")

    # Execute Commands
    for cmd in protocol.commands:
        pip = f"pip_{cmd.pipette}"

        if cmd.command_type == "aspirate":
            lw = labware_map.get(cmd.labware)
            if not lw:
                raise ValueError(f"Labware '{cmd.labware}' not found")
            lines.append(f"    {pip}.aspirate({cmd.volume}, {lw}['{cmd.well}'])")

        elif cmd.command_type == "dispense":
            lw = labware_map.get(cmd.labware)
            if not lw:
                raise ValueError(f"Labware '{cmd.labware}' not found")
            lines.append(f"    {pip}.dispense({cmd.volume}, {lw}['{cmd.well}'])")

        elif cmd.command_type == "mix":
            lw = labware_map.get(cmd.labware)
            if not lw:
                raise ValueError(f"Labware '{cmd.labware}' not found")
            lines.append(f"    {pip}.mix({cmd.repetitions}, {cmd.volume}, {lw}['{cmd.well}'])")

        elif cmd.command_type == "blow_out":
            if cmd.labware and cmd.well:
                lw = labware_map.get(cmd.labware)
                if not lw:
                    raise ValueError(f"Labware '{cmd.labware}' not found")
                lines.append(f"    {pip}.blow_out({lw}['{cmd.well}'])")
            else:
                lines.append(f"    {pip}.blow_out()")

        elif cmd.command_type == "touch_tip":
            lw = labware_map.get(cmd.labware)
            if not lw:
                raise ValueError(f"Labware '{cmd.labware}' not found")
            lines.append(f"    {pip}.touch_tip({lw}['{cmd.well}'])")

        elif cmd.command_type == "air_gap":
            lines.append(f"    {pip}.air_gap({cmd.volume})")

        elif cmd.command_type == "pick_up_tip":
            if cmd.labware and cmd.well:
                lw = labware_map.get(cmd.labware)
                if not lw:
                    raise ValueError(f"Labware '{cmd.labware}' not found")
                lines.append(f"    {pip}.pick_up_tip({lw}['{cmd.well}'])")
            else:
                lines.append(f"    {pip}.pick_up_tip()")

        elif cmd.command_type == "drop_tip":
            if cmd.labware and cmd.well:
                lw = labware_map.get(cmd.labware)
                if not lw:
                    raise ValueError(f"Labware '{cmd.labware}' not found")
                lines.append(f"    {pip}.drop_tip({lw}['{cmd.well}'])")
            else:
                lines.append(f"    {pip}.drop_tip()")

        elif cmd.command_type == "return_tip":
            lines.append(f"    {pip}.return_tip()")

        # Flow control commands
        elif cmd.command_type == "pause":
            # Escape single quotes in message
            msg = cmd.message.replace("'", "\\'")
            lines.append(f"    protocol.pause('{msg}')")

        elif cmd.command_type == "delay":
            if cmd.minutes:
                lines.append(f"    protocol.delay(minutes={cmd.minutes})")
            elif cmd.seconds:
                lines.append(f"    protocol.delay(seconds={cmd.seconds})")

        elif cmd.command_type == "comment":
            # Escape single quotes in message
            msg = cmd.message.replace("'", "\\'")
            lines.append(f"    protocol.comment('{msg}')")

        elif cmd.command_type == "transfer":
            src_lw = labware_map.get(cmd.source_labware)
            dst_lw = labware_map.get(cmd.dest_labware)
            if not src_lw or not dst_lw:
                raise ValueError(f"Labware '{cmd.source_labware}' or '{cmd.dest_labware}' not found")

            args = [
                str(cmd.volume),
                f"{src_lw}['{cmd.source_well}']",
                f"{dst_lw}['{cmd.dest_well}']",
            ]
            kwargs = []
            if cmd.new_tip != "always":
                kwargs.append(f"new_tip='{cmd.new_tip}'")
            if cmd.mix_before:
                # Ensure tuple format (reps, volume) for Opentrons API
                kwargs.append(f"mix_before=({cmd.mix_before[0]}, {cmd.mix_before[1]})")
            if cmd.mix_after:
                # Ensure tuple format (reps, volume) for Opentrons API
                kwargs.append(f"mix_after=({cmd.mix_after[0]}, {cmd.mix_after[1]})")

            all_args = ", ".join(args + kwargs)
            lines.append(f"    {pip}.transfer({all_args})")

        elif cmd.command_type == "distribute":
            src_lw = labware_map.get(cmd.source_labware)
            dst_lw = labware_map.get(cmd.dest_labware)
            if not src_lw or not dst_lw:
                raise ValueError(f"Labware '{cmd.source_labware}' or '{cmd.dest_labware}' not found")

            dest_wells_str = ", ".join([f"{dst_lw}['{w}']" for w in cmd.dest_wells])
            args = [
                str(cmd.volume),
                f"{src_lw}['{cmd.source_well}']",
                f"[{dest_wells_str}]",
            ]
            kwargs = []
            if cmd.new_tip != "once":
                kwargs.append(f"new_tip='{cmd.new_tip}'")

            all_args = ", ".join(args + kwargs)
            lines.append(f"    {pip}.distribute({all_args})")

        elif cmd.command_type == "consolidate":
            src_lw = labware_map.get(cmd.source_labware)
            dst_lw = labware_map.get(cmd.dest_labware)
            if not src_lw or not dst_lw:
                raise ValueError(f"Labware '{cmd.source_labware}' or '{cmd.dest_labware}' not found")

            source_wells_str = ", ".join([f"{src_lw}['{w}']" for w in cmd.source_wells])
            args = [
                str(cmd.volume),
                f"[{source_wells_str}]",
                f"{dst_lw}['{cmd.dest_well}']",
            ]
            kwargs = []
            if cmd.new_tip != "once":
                kwargs.append(f"new_tip='{cmd.new_tip}'")

            all_args = ", ".join(args + kwargs)
            lines.append(f"    {pip}.consolidate({all_args})")

        # Module commands
        elif cmd.command_type == "set_temperature":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.set_temperature({cmd.celsius})")

        elif cmd.command_type == "wait_for_temperature":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.await_temperature({cmd.celsius})")

        elif cmd.command_type == "deactivate":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.deactivate()")

        elif cmd.command_type == "engage_magnets":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            if cmd.height:
                lines.append(f"    {mod}.engage(height_from_base={cmd.height})")
            else:
                lines.append(f"    {mod}.engage()")

        elif cmd.command_type == "disengage_magnets":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.disengage()")

        elif cmd.command_type == "set_shake_speed":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            if cmd.rpm == 0:
                lines.append(f"    {mod}.stop_shaking()")
            else:
                lines.append(f"    {mod}.set_and_wait_for_shake_speed({cmd.rpm})")

        elif cmd.command_type == "open_latch":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.open_labware_latch()")

        elif cmd.command_type == "close_latch":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.close_labware_latch()")

        elif cmd.command_type == "open_lid":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.open_lid()")

        elif cmd.command_type == "close_lid":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.close_lid()")

        elif cmd.command_type == "set_block_temperature":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            args = [str(cmd.celsius)]
            if cmd.hold_time_seconds:
                args.append(f"hold_time_seconds={cmd.hold_time_seconds}")
            if cmd.hold_time_minutes:
                args.append(f"hold_time_minutes={cmd.hold_time_minutes}")
            lines.append(f"    {mod}.set_block_temperature({', '.join(args)})")

        elif cmd.command_type == "set_lid_temperature":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            lines.append(f"    {mod}.set_lid_temperature({cmd.celsius})")

        elif cmd.command_type == "run_profile":
            mod = module_map.get(cmd.module)
            if not mod:
                raise ValueError(f"Module '{cmd.module}' not found")
            steps_str = str(cmd.steps)
            lines.append(f"    {mod}.execute_profile(steps={steps_str}, repetitions={cmd.repetitions})")

        else:
            raise ValueError(f"Unknown command type: {cmd.command_type}")

    return "\n".join(lines)


def simulate_script(script_code: str) -> tuple[bool, str, list]:
    """Runs the script through the Opentrons simulator.

    Returns:
        Tuple of (success, formatted_log, raw_runlog)
        - success: True if simulation passed
        - formatted_log: Human-readable simulation log or error message
        - raw_runlog: List of command dicts from simulator (empty on failure)
    """
    protocol_file = io.StringIO(script_code)
    try:
        # Suppress simulator stderr output (deck calibration warnings, etc.)
        stderr_capture = io.StringIO()
        with redirect_stderr(stderr_capture):
            runlog, _ = simulate.simulate(protocol_file)
        # Format the run log into readable output
        log_lines = ["Simulation completed successfully.", "", "Run Log:"]
        for entry in runlog:
            if isinstance(entry, dict):
                msg = entry.get('payload', {}).get('text', str(entry))
                log_lines.append(f"  {msg}")
            else:
                log_lines.append(f"  {entry}")
        return True, "\n".join(log_lines), runlog
    except Exception as e:
        return False, f"Simulation failed: {str(e)}", []


class ProtocolAgent:
    def __init__(self, config_path: str = "lab_config.json"):
        self.config_path = config_path
        self.parser = ProtocolParser(config_path=config_path)

    @staticmethod
    def _infer_source_containers(spec) -> list:
        """Identify labware wells that are only aspirated from, never dispensed into.

        These are source containers (reservoirs, tube racks) that the scientist
        fills before running the protocol. Returns list of (labware, well, substance).
        """
        # Track which wells are sources vs destinations
        source_wells = {}   # (labware, well) → substance
        dest_wells = set()  # (labware, well)

        liquid_actions = {"transfer", "distribute", "consolidate",
                          "serial_dilution", "aspirate"}

        for step in spec.steps:
            if step.action not in liquid_actions:
                continue

            # Source side
            if step.source:
                desc = step.source.description
                wells = []
                if step.source.well:
                    wells = [step.source.well]
                elif step.source.wells:
                    wells = step.source.wells
                for w in wells:
                    key = (desc, w)
                    if key not in source_wells:
                        source_wells[key] = step.substance.value if step.substance else None

            # Destination side
            if step.destination:
                desc = step.destination.description
                wells = []
                if step.destination.well:
                    wells = [step.destination.well]
                elif step.destination.wells:
                    wells = step.destination.wells
                # For well ranges, just mark the description as having destinations
                if step.destination.well_range:
                    dest_wells.add((desc, "__range__"))
                for w in wells:
                    dest_wells.add((desc, w))

        # Source-only wells: aspirated from but never dispensed into
        # Also exclude wells whose labware appears as a destination anywhere
        #  (e.g., serial dilution where the same plate is both source and dest)
        dest_labware = {lw for lw, _ in dest_wells}
        already_tracked = {(ic.labware, ic.well) for ic in spec.initial_contents}

        results = []
        seen = set()
        for (labware, well), substance in source_wells.items():
            if labware in dest_labware:
                continue  # Same labware used as dest elsewhere — not a pure source
            if (labware, well) in already_tracked:
                continue  # Already in initial_contents from LLM extraction
            if (labware, well) in seen:
                continue
            seen.add((labware, well))
            results.append((labware, well, substance))

        return results

    def _confirm_labware_assignments(self, spec) -> Optional[dict]:
        """Interactive confirmation of labware description → config label assignments.

        Shows all assignments at once. User picks a number to change any mapping,
        or presses Enter to confirm all. Unresolved refs show as '???' and must
        be assigned before confirming.

        Returns dict of {description: confirmed_label} or None if aborted.
        """
        # Collect unique (description, resolved_label) pairs
        assignments = []
        seen = set()
        for step in spec.steps:
            for ref in [step.source, step.destination]:
                if ref and ref.description not in seen:
                    seen.add(ref.description)
                    assignments.append({
                        "description": ref.description,
                        "resolved_label": ref.resolved_label,
                    })

        if not assignments:
            return {}

        available_labels = list(self.parser.config.get("labware", {}).keys())
        # Start with LLM suggestions as current assignments
        current = {a["description"]: a["resolved_label"] for a in assignments}

        while True:
            # Display all assignments
            _log(f"\n  {C.label('Labware assignments')}:")
            all_resolved = True
            for i, a in enumerate(assignments, 1):
                desc = a["description"]
                label = current.get(desc)
                if label:
                    load_name = self.parser.config["labware"][label].get("load_name", "")
                    _log(f"    [{i}] \"{desc}\" → {C.success(label)} ({C.dim(load_name)})")
                else:
                    _log(f"    [{i}] \"{desc}\" → {C.warning('??? (no match)')}")
                    all_resolved = False

            # Prompt
            if all_resolved:
                response = _prompt_input("Pick a number to change, or Enter to confirm all (q=quit): ").lower()
            else:
                _log(f"  {C.dim('Assign all ??? items before confirming. If your config is missing')}")
                _log(f"  {C.dim('labware, quit (q) and add it to your config.json, then re-run.')}")
                response = _prompt_input("Pick a number to assign (q=quit): ").lower()

            if response == 'q':
                _log("  Aborted.")
                return None
            elif response == '':
                if all_resolved:
                    # Warn if multiple descriptions map to the same label
                    label_counts = {}
                    for desc, label in current.items():
                        label_counts.setdefault(label, []).append(desc)
                    for label, descs in label_counts.items():
                        if len(descs) > 1:
                            _log(f"  {C.warning('Note:')} \"{descs[0]}\" and \"{descs[1]}\" "
                                 f"both map to {label} — is this the same labware?")
                    return current
                else:
                    unresolved = [a["description"] for a in assignments if not current.get(a["description"])]
                    _log(f"  Cannot confirm — unresolved: {unresolved}")
                    continue
            elif response.isdigit():
                idx = int(response) - 1
                if 0 <= idx < len(assignments):
                    desc = assignments[idx]["description"]
                    _log(f"\n    Reassigning \"{desc}\":")
                    for j, label in enumerate(available_labels, 1):
                        load_name = self.parser.config["labware"][label].get("load_name", "")
                        marker = " ←" if label == current.get(desc) else ""
                        _log(f"      {j}. {label} ({C.dim(load_name)}){marker}")
                    pick = _prompt_input(f"Pick label (1-{len(available_labels)}), or Enter to cancel: ")
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

    def _confirm_provenance_items(self, spec, confirmable: list) -> Optional[bool]:
        """Interactive per-item confirmation for uncertain provenance claims.

        For each item in the confirmation queue, the user can:
          - Accept (Enter/y) — keep the value as-is
          - Reject (n) — abort the pipeline
          - Edit (e) — type a replacement value

        Returns True if all items accepted/edited, None if aborted.
        """
        from .extractor import _find_provenance_reason

        _log(f"\n  {C.label('Confirm uncertain values:')}")
        _log(f"  For each item: (Enter) accept, (e) edit, (s) skip all and accept, (q) quit\n")

        edits = []
        for i, w in enumerate(confirmable, 1):
            # --- Initial volume items (null volume_ul in initial_contents) ---
            if w.get("type") == "initial_volume":
                default = w["default_volume"]
                _log(f"  [{i}/{len(confirmable)}] Initial contents: "
                     f"{w['labware']} {w['well']}, \"{w['substance']}\" — "
                     f"no volume stated")
                _log(f"    1) Enter volume")
                _log(f"    2) Accept default ({default:.0f}uL, well capacity)")
                _log(f"    3) Exit (edit instruction and run again)")

                response = _prompt_input("Pick 1-3: ").strip()
                if response == '3':
                    return None
                elif response == '1':
                    new_val = _prompt_input(f"Volume in uL: ").strip()
                    if new_val:
                        try:
                            vol = float(new_val)
                            # Write back to spec
                            for ic in spec.initial_contents:
                                if ic.labware == w['labware'] and ic.well == w['well']:
                                    ic.volume_ul = vol
                            _log(f"    → {C.success(f'{vol}uL')}")
                        except ValueError:
                            _log(f"    Invalid number, using default ({default:.0f}uL).")
                    else:
                        _log(f"    Empty input, using default ({default:.0f}uL).")
                else:
                    # '2', Enter, or anything else → accept default
                    pass
                continue

            # --- Standard provenance items ---
            step_obj = next((s for s in spec.steps if s.order == w['step']), None)
            reason = _find_provenance_reason(step_obj, w['field']) if step_obj else None

            # Describe step by action + substance
            if step_obj:
                action = step_obj.action.upper()
                substance = f" ({step_obj.substance.value})" if step_obj.substance else ""
                step_desc = f"Step {w['step']} {action}{substance}"
            else:
                step_desc = f"Step {w['step']}"

            _log(f"  [{i}/{len(confirmable)}] {step_desc}, {w['field']}: "
                 f"{C.warning(w['value'])}")
            if reason:
                _log(f"    Inferred: {reason}")

            response = _prompt_input("Enter=accept, e=edit, s=accept all, q=quit: ").lower()

            if response == 'q':
                return None
            elif response == 's':
                _log(f"  {C.dim('Accepting all remaining items.')}")
                break
            elif response == 'e':
                new_val = _prompt_input(f"New value for {w['field']}: ")
                if new_val:
                    edits.append((w, new_val))
                    _log(f"    → {C.success(new_val)}")
                else:
                    _log(f"    Empty input, keeping original.")
            elif response in ('', 'y', 'yes'):
                pass  # accepted
            else:
                _log(f"    Unknown input, treating as accept.")

        # Apply edits to spec
        if edits:
            spec = self._apply_provenance_edits(spec, edits)
            _log(f"\n  {len(edits)} value(s) edited.")

        return True

    def _apply_provenance_edits(self, spec, edits: list):
        """Apply user edits from the provenance confirmation flow.

        Each edit is (warning_dict, new_value_str). Modifies spec in place.
        """
        for w, new_val in edits:
            step = next((s for s in spec.steps if s.order == w['step']), None)
            if not step:
                continue

            field = w['field']
            if field == "volume" and step.volume:
                try:
                    step.volume.value = float(new_val)
                    step.volume.provenance.source = "instruction"
                    step.volume.provenance.confidence = 1.0
                    step.volume.provenance.reason = "User-edited during confirmation"
                except ValueError:
                    pass
            elif field == "substance" and step.substance:
                step.substance.value = new_val
                step.substance.provenance.source = "instruction"
                step.substance.provenance.confidence = 1.0
                step.substance.provenance.reason = "User-edited during confirmation"
            elif field == "duration" and step.duration:
                try:
                    step.duration.value = float(new_val)
                    step.duration.provenance.source = "instruction"
                    step.duration.provenance.confidence = 1.0
                    step.duration.provenance.reason = "User-edited during confirmation"
                except ValueError:
                    pass
            elif field == "temperature" and step.temperature:
                try:
                    step.temperature.value = float(new_val)
                    step.temperature.provenance.source = "instruction"
                    step.temperature.provenance.confidence = 1.0
                    step.temperature.provenance.reason = "User-edited during confirmation"
                except ValueError:
                    pass
            elif field == "composition":
                # Can't meaningfully edit composition — skip
                pass
            # Post-action fields
            elif step.post_actions:
                for pa in step.post_actions:
                    if field.startswith(pa.action) and "volume" in field and pa.volume:
                        try:
                            pa.volume.value = float(new_val)
                            pa.volume.provenance.source = "instruction"
                            pa.volume.provenance.confidence = 1.0
                            pa.volume.provenance.reason = "User-edited during confirmation"
                        except ValueError:
                            pass

        return spec

    def run_pipeline(self, prompt: str, csv_path: str = None, skip_validation: bool = False,
                     full_confirmation: bool = False, confirmation_threshold: float = 0.7,
                     verbose: bool = False) -> Optional[PipelineResult]:
        """Run the protocol generation pipeline.

        New architecture (v2):
          Stage 0: Input validation
          Stage 1: LLM reasons through instruction → ProtocolSpec (one LLM call)
          Stage 2: Hallucination guard + sufficiency check + gap filling
          Stage 3: User confirms the spec
          Stage 4: Deterministic: spec + config → ProtocolSchema (no LLM)
          Stage 5: Deterministic: ProtocolSchema → Python script (no LLM)
          Stage 6: Opentrons simulation
          Stage 7: Intent verification (LLM, optional safety net)

        Only Stage 1 uses an LLM for generation. Stages 4-5 are deterministic,
        so user-specified volumes can never be corrupted.

        Falls back to the old LLM-based generation if the deterministic path fails.
        """
        from datetime import datetime
        from .extractor import SemanticExtractor

        # DEV ONLY — remove before public release.
        # Intermediate state log: accumulates spec snapshots throughout the
        # pipeline, written to disk on completion or failure for inspection.
        state_log = {
            "timestamp": datetime.now().isoformat(),
            "input": {"instruction": prompt},
        }

        def _save_state_log(failed_at: str = None):
            if failed_at:
                state_log["failed_at"] = failed_at
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = f"pipeline_state_{ts}.json"
            with open(path, 'w') as f:
                json.dump(state_log, f, indent=2, default=str)
            _log(f"  {C.dim(f'State log: {path}')}")

        # Stage 1: Validate config and instruction
        _stage("[Stage 1/8] Validating instruction and config...")
        try:
            self.parser.load_config()
            state_log["input"]["config"] = self.parser.config
            _log(f"  {C.success('Config validated.')}")
        except Exception as e:
            _log(f"  {C.error('Config validation failed:')}")
            _log(f"    {e}")
            _save_state_log("stage_1_config")
            return None

        if not skip_validation:
            from .input_validator import InputValidator
            validator = InputValidator()
            try:
                validation = validator.classify(prompt)
            except Exception as e:
                _log(f"  {C.error(str(e))}")
                return None

            if not validation.is_valid_protocol:
                if validation.classification == "QUESTION":
                    _log("  This looks like a question, not a protocol instruction.")
                    _log("  Try rephrasing as an action: 'Transfer 100uL from A1 to B1'")
                elif validation.classification == "AMBIGUOUS":
                    _log("  This instruction is too vague to generate a protocol.")
                    _log("  Try adding specific volumes, wells, and labware names.")
                elif validation.classification == "INVALID":
                    _log("  This doesn't appear to be a liquid-handling protocol.")
                    _log("  The OT-2 can only pipette — it can't centrifuge, read absorbance, etc.")
                if validation.suggestion:
                    _log(f"  Suggestion: {validation.suggestion}")
                return None
            _log(f"  {C.success('Input validated as protocol instruction.')}")
        else:
            _log("  Skipping input validation.")

        # Stage 2: Reason through the instruction → ProtocolSpec
        _stage("[Stage 2/8] Reasoning through protocol instruction...")
        extractor = SemanticExtractor(
            client=self.parser.client,
            model_name=self.parser.model_name
        )
        spec = extractor.extract(prompt, self.parser.config)

        if spec is None:
            _log("\n  Could not extract a protocol from your instruction.")
            _log("  This can happen if:")
            _log("    - The instruction is too vague (add volumes, wells, labware names)")
            _log("    - The API key is invalid or out of credits")
            _log("    - The instruction isn't about liquid handling")
            _log("  Try: 'Transfer 100uL from source_plate A1 to dest_plate B1'")
            _save_state_log("stage_2_extraction")
            return None

        state_log["stage_2_extraction"] = spec.model_dump()

        _log(f"  {C.label('Protocol type:')} {spec.protocol_type or 'ad-hoc'}")
        _log(f"  {C.label('Steps:')} {len(spec.steps)}")
        if spec.reasoning:
            _log(f"  {C.label('Reasoning:')}")
            import re
            reasoning = spec.reasoning.strip()
            parts = re.split(r'(?=\d+\.\s)', reasoning)
            for part in parts:
                part = part.strip()
                if not part:
                    continue
                sub_parts = re.split(r'(?=\s*-\s)', part, maxsplit=0)
                for i, sub in enumerate(sub_parts):
                    sub = sub.strip()
                    if not sub:
                        continue
                    if i == 0:
                        _log(_wrap(C.info(sub), indent="    ", width=LINE_WIDTH))
                    else:
                        _log(_wrap(C.info(f"- {sub.lstrip('- ')}"), indent="      ", width=LINE_WIDTH))

        if spec.initial_contents:
            _log(f"  {C.dim(f'Initial state: {len(spec.initial_contents)} wells/tubes have contents')}")

        # Stage 3: Validate extraction + check sufficiency + fill gaps
        _stage("[Stage 3/8] Validating and completing specification...")

        # Provenance verification — check instruction/config claims
        provenance_warnings = extractor.verify_provenance_claims(spec, prompt, self.parser.config)
        fabrications = [w for w in provenance_warnings if w["severity"] == "fabrication"]

        state_log["stage_3_provenance"] = provenance_warnings

        if fabrications:
            _log(f"\n  {C.error(f'{len(fabrications)} fabrication error(s) detected:')}")
            for w in fabrications:
                _log(f"    {C.error('!')} {w['message']}")
            _log(f"\n  The LLM put values in wrong fields or misattributed their source.")
            _log(f"  This usually means temperatures ended up in volume fields, or")
            _log(f"  the LLM computed a derived value and tagged it as 'from instruction'.")
            _log(f"  Re-run, or try a more explicit instruction.")
            _save_state_log("stage_3_provenance")
            return None

        non_fabrications = [w for w in provenance_warnings if w["severity"] != "fabrication"]
        if non_fabrications:
            _log(f"  {C.dim(f'{len(non_fabrications)} provenance note(s) — will review during confirmation.')}")

        # Sufficiency check
        gaps = extractor.missing_fields(spec)
        if gaps:
            _log(f"  Gaps found: {len(gaps)}")
            for g in gaps:
                _log(f"    - {g}")
            spec, fills = extractor.fill_gaps(spec, self.parser.config)
            if fills:
                _log(f"  Filled from config:")
                for f in fills:
                    _log(f"    - {f}")
            remaining_gaps = extractor.missing_fields(spec)
            if remaining_gaps:
                _log(f"  Could not fill all gaps from config:")
                for g in remaining_gaps:
                    _log(f"    - {g}")
                _log("  Attempting self-refinement (re-reasoning with gap feedback)...")
                refined = extractor.refine(spec, remaining_gaps, prompt, self.parser.config)
                if refined:
                    spec = refined
                    final_gaps = extractor.missing_fields(spec)
                    if final_gaps:
                        _log(f"  Refinement could not resolve all gaps:")
                        for g in final_gaps:
                            _log(f"    - {g}")
                        _log("  Try: add the missing details to your instruction (volumes, wells, labware).")
                        state_log["stage_3_gaps"] = {"initial": gaps, "after_fill": remaining_gaps, "after_refine": final_gaps}
                        _save_state_log("stage_3_gaps")
                        return None
                    _log("  Refinement resolved all gaps.")
                else:
                    _log("  Self-refinement did not produce a valid specification.")
                    _log("  The LLM could not fill the gaps listed above even after retrying.")
                    _log("  Try: add more detail to your instruction, or simplify the protocol.")
                    state_log["stage_3_gaps"] = {"initial": gaps, "after_fill": remaining_gaps, "refine_failed": True}
                    _save_state_log("stage_3_gaps")
                    return None
            else:
                _log("  Gaps filled from config.")
        else:
            _log(f"  {C.dim('Specification is complete.')}")

        if spec.explicit_volumes:
            _log(f"  {C.dim(f'Locked volumes: {spec.explicit_volumes}')}")

        # Infer source containers: labware that is aspirated from but never
        # dispensed into must be pre-filled by the scientist.
        source_only = self._infer_source_containers(spec)
        if source_only:
            _log("\n  Inferred source containers (pre-filled by you before running):")
            for labware, well, substance in source_only:
                sub = f" ({substance})" if substance else ""
                _log(f"    - {labware} well {well}{sub}")
            if sys.stdin.isatty():
                response = _prompt_input("Is this correct? [Y/n]: ").lower()
                if response in ('n', 'no'):
                    _log("  Aborted. Adjust your instruction to clarify source containers.")
                    state_log["stage_3_sources"] = [{"labware": lw, "well": w, "substance": s} for lw, w, s in source_only]
                    _save_state_log("stage_3_sources")
                    return None
            # Add confirmed sources to initial_contents so well tracker doesn't warn
            from .extractor import WellContents
            for labware, well, substance in source_only:
                already = any(ic.labware == labware and ic.well == well
                              for ic in spec.initial_contents)
                if not already:
                    spec.initial_contents.append(
                        WellContents(labware=labware, well=well,
                                     substance=substance or "reagent")
                    )

        # Stage 3.5: Resolve labware references (description → config label)
        _stage("[Stage 3.5/8] Resolving labware references...")
        from .extractor import LabwareResolver
        resolver = LabwareResolver(
            config=self.parser.config,
            client=extractor.client,
            model_name=extractor.model_name,
        )
        spec = resolver.resolve(spec)

        state_log["stage_3.5_resolution"] = {
            ref.description: ref.resolved_label
            for step in spec.steps
            for ref in [step.source, step.destination]
            if ref
        }

        # Confirm tentative assignments with user (includes unresolved refs)
        if sys.stdin.isatty():
            confirmed = self._confirm_labware_assignments(spec)
            if confirmed is None:
                _save_state_log("stage_3.5_resolution")
                return None
            for step in spec.steps:
                for ref in [step.source, step.destination]:
                    if ref and ref.description in confirmed:
                        ref.resolved_label = confirmed[ref.description]

        # Check if any step LocationRef is still unresolved — that's a hard error.
        unresolved_refs = []
        for step in spec.steps:
            for ref in [step.source, step.destination]:
                if ref and not ref.resolved_label and ref.description not in unresolved_refs:
                    unresolved_refs.append(ref.description)

        if unresolved_refs:
            _log(f"\n  {C.error('No config match for:')} {unresolved_refs}")
            _log(f"  Add appropriate labware to your config.json for these, then re-run.")
            _save_state_log("stage_3.5_unresolved")
            return None

        _log(f"  {C.success('All labware resolved.')}")

        # Stage 4: Constraint checking (deterministic, no LLM)
        _stage("[Stage 4/8] Checking hardware constraints...")
        from .constraints import ConstraintChecker
        checker = ConstraintChecker(self.parser.config)
        constraint_result = checker.check_all(spec)

        state_log["stage_4_constraints"] = {
            "errors": [str(v) for v in constraint_result.errors],
            "warnings": [str(w) for w in constraint_result.warnings],
        }

        if constraint_result.warnings:
            for w in constraint_result.warnings:
                _log(f"  {w}")

        if constraint_result.has_errors:
            _log(f"\n  {C.error('HARDWARE CONFLICTS DETECTED')} ({len(constraint_result.errors)}):")
            _log("  " + "-" * 56)
            for v in constraint_result.errors:
                _log(f"  {v}")
                _log()

            if sys.stdin.isatty():
                _log("  These conflicts mean the protocol may not execute correctly.")
                response = _prompt_input("Proceed anyway? [y/N]: ").lower()
                if response not in ('y', 'yes'):
                    _log("  Aborted. Fix your config or instruction and retry.")
                    _save_state_log("stage_4_constraints")
                    return None
                _log(f"  {C.dim('Proceeding with constraint adjustments.')}")
            else:
                _log("  Cannot proceed with hardware conflicts in non-interactive mode.")
                _save_state_log("stage_4_constraints")
                return None
        else:
            _log(f"  {C.success('All constraints satisfied.')}")

        # Build full confirmation queue: provenance warnings + null initial volumes
        confirmable = [w for w in non_fabrications if w["severity"] in ("unverified", "low_confidence")]

        from .constraints import WellStateTracker
        for ic in spec.initial_contents:
            if ic.volume_ul is None:
                fallback = WellStateTracker._get_well_capacity_static(
                    ic.labware, self.parser.config) or 15000.0
                confirmable.append({
                    "type": "initial_volume",
                    "labware": ic.labware,
                    "well": ic.well,
                    "substance": ic.substance,
                    "default_volume": fallback,
                })

        # Confirm with user
        # (fabrications already blocked in Stage 3 — only non-fabrication warnings remain)
        _log(extractor.format_for_confirmation(
            spec, non_fabrications,
            threshold=confirmation_threshold,
            full=full_confirmation,
            extra_confirmable=[w for w in confirmable if w.get("type") == "initial_volume"],
        ))

        if sys.stdin.isatty():
            if confirmable and not full_confirmation:
                _log(f"\n  {len(confirmable)} item(s) need confirmation.")
                accepted = self._confirm_provenance_items(spec, confirmable)
                if accepted is None:
                    _log("  Aborted by user.")
                    return None
            else:
                response = _prompt_input("Proceed with this specification? [Y/n]: ").lower()
                if response in ('n', 'no'):
                    _log("  Aborted by user.")
                    return None

        # Stage 5: Deterministic spec → ProtocolSchema
        _stage("[Stage 5/8] Converting specification to protocol schema...")

        state_log["stage_5_spec"] = spec.model_dump()

        try:
            from .extractor import CompleteProtocolSpec
            complete_spec = CompleteProtocolSpec.model_validate(spec.model_dump())
            protocol_schema = extractor.spec_to_schema(complete_spec, self.parser.config)
            _log(f"  {C.dim('Schema generated.')}")
        except Exception as e:
            err = str(e)
            _log(f"  Schema conversion failed.")
            if "not found" in err.lower():
                _log(f"    A labware or module reference could not be resolved: {err}")
                _log(f"    Check that your config labels match the specification above.")
            else:
                _log(f"    Detail: {err}")
            _log(f"    This is a deterministic step — the specification likely has an inconsistency.")
            _save_state_log("stage_5_schema")
            return None

        # Stage 6: Deterministic schema → Python script
        _stage("[Stage 6/8] Generating Python script...")
        try:
            script = generate_python_script(protocol_schema)
            _log(f"  {C.dim('Script generated.')}")
        except ValueError as e:
            err = str(e)
            _log(f"  Script generation failed.")
            _log(f"    Detail: {err}")
            _log(f"    Try: simplify your instruction or check config slot assignments.")
            _save_state_log("stage_6_script")
            return None

        # Save script for debugging regardless of simulation outcome
        debug_script = f"debug_script_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py"
        with open(debug_script, 'w') as f:
            f.write(script)
        _log(f"  {C.dim(f'Debug: script saved to {debug_script}')}")

        # Stage 7: Opentrons simulation
        _stage("[Stage 7/8] Running Opentrons simulation...")
        success, simulation_log, runlog = simulate_script(script)

        if not success:
            if "errorType=" in simulation_log:
                import re
                error_match = re.search(r"errorType='([^']+)'", simulation_log)
                error_detail_match = re.search(r"errorInfo=\{[^}]*'detail':\s*'([^']+)'", simulation_log)
                error_type = error_match.group(1) if error_match else "Unknown"
                error_detail = error_detail_match.group(1) if error_detail_match else simulation_log[:300]
                _log(f"  Simulation failed: {error_type}")
                _log(f"    Detail: {error_detail[:200]}")
            else:
                _log(f"  Simulation failed: {simulation_log[:500]}")
            _log(f"  The generated Python script did not pass the Opentrons simulator.")
            _log(f"  Check {debug_script} to see the generated code.")
            _save_state_log("stage_7_simulation")
            return None

        _log(f"  {C.success('Simulation passed.')}")

        # Stage 8: Intent verification (safety net)
        _stage("[Stage 8/8] Verifying protocol matches original intent...")
        from .spinner import Spinner
        # Build full labware mapping: description → config label → load name
        labware_mapping = {}
        for desc, label in state_log.get("stage_3.5_resolution", {}).items():
            if label:
                load_name = self.parser.config.get("labware", {}).get(label, {}).get("load_name", "")
                labware_mapping[desc] = {"config_label": label, "load_name": load_name}

        with Spinner("Verifying intent match..."):
            verification = verify_intent_match(prompt, protocol_schema, labware_mapping)

        state_log["stage_8_verification"] = {
            "matches": verification.matches,
            "confidence": verification.confidence,
            "issues": verification.issues,
            "summary": verification.summary,
        }

        if verification.matches and verification.confidence >= 0.7:
            _log(f"  {C.success(f'Intent verification passed (confidence: {verification.confidence:.0%})')}")
            _save_state_log()
            return PipelineResult(
                script=script,
                simulation_log=simulation_log,
                protocol_schema=protocol_schema,
                runlog=runlog,
                config=self.parser.config
            )
        else:
            _log(f"  Intent mismatch (confidence: {verification.confidence:.0%})")
            if verification.issues:
                _log(f"  Issues: {'; '.join(verification.issues)}")
            _log(f"  Summary: {verification.summary}")
            _log(f"  The generated protocol may not match what you asked for.")
            _save_state_log("stage_8_verification")
            return None


if __name__ == "__main__":
    import sys
    from nl2protocol.cli import main

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append('--help')

    sys.exit(main())
