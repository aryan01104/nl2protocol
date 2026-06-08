"""
codegen.py — ProtocolSchema → Opentrons Python script (deterministic).

Pure translation: walks the schema's modules / labware / pipettes /
commands and emits a runnable Opentrons Python protocol. No LLM, no
user interaction, no side effects beyond constructing the script string.
"""

from typing import Optional

from ..models import ProtocolSchema


def generate_python_script(
    protocol: ProtocolSchema,
    step_summaries: Optional[list] = None,
):
    """Generates a valid Opentrons Python script from the Pydantic schema.

    Pre:    `protocol` is a Pydantic-validated ProtocolSchema.
            `step_summaries`, when provided, is the list returned by
            `spec_to_schema` carrying `commands_generated` per spec step
            in the same order they emit. When omitted, the function
            returns just the script string (legacy single-return shape).
    Post:   When `step_summaries` is None: returns the script as a
            single \\n-joined string (back-compat).
            When provided: returns `(script_str, step_line_map)` where
            step_line_map is `{step_idx: [line_idx_0, line_idx_1, ...]}`
            covering ONLY the lines produced by each step's commands.
            Prelude lines (module/labware/pipette loads) aren't mapped to
            any step and don't appear in the map.

    Used by the live mode + the static report's spec→code linkage hover
    pair: each script line tagged with its step's id, hovering a
    Validated Spec block highlights all matching script lines, hovering
    a script line highlights its source step block.
    """

    lines = [
        "from opentrons import protocol_api",
        "",
        f"metadata = {{'protocolName': '{protocol.protocol_name}', 'author': '{protocol.author}', 'apiLevel': '2.15'}}",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
    ]

    # Order matters: Modules first, then labware (some may load onto modules), then pipettes
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

    labware_map = {}
    for lw in protocol.labware:
        var_name = f"lw_{lw.slot}"
        label_arg = f", label='{lw.label}'" if lw.label else ""

        if lw.on_module:
            mod_var = module_map.get(lw.on_module)
            if not mod_var:
                raise ValueError(f"Module '{lw.on_module}' not found for labware '{lw.label or lw.slot}'")
            lines.append(f"    {var_name} = {mod_var}.load_labware('{lw.load_name}'{label_arg})")
        else:
            lines.append(f"    {var_name} = protocol.load_labware('{lw.load_name}', '{lw.slot}'{label_arg})")

        labware_map[lw.slot] = var_name
        if lw.label:
            labware_map[lw.label] = var_name
    lines.append("")

    for pip in protocol.pipettes:
        tiprack_vars = [labware_map.get(tr) for tr in pip.tipracks if labware_map.get(tr)]
        tiprack_str = f", tip_racks=[{', '.join(tiprack_vars)}]" if tiprack_vars else ""
        lines.append(f"    pip_{pip.mount} = protocol.load_instrument('{pip.model}', '{pip.mount}'{tiprack_str})")
    lines.append("")

    # Execute Commands. Track per-step line ranges so the live + static
    # surfaces can highlight (Step block ↔ script lines) on hover.
    step_line_map: dict = {}
    cmd_to_step_idx: dict = {}
    if step_summaries:
        running_cmd_idx = 0
        for s in step_summaries:
            n = int(s.get("commands_generated", 0) or 0)
            step_idx = (s.get("step", 1) or 1) - 1   # spec uses 1-based step.order
            for _ in range(n):
                cmd_to_step_idx[running_cmd_idx] = step_idx
                running_cmd_idx += 1

    for cmd_idx, cmd in enumerate(protocol.commands):
        step_idx = cmd_to_step_idx.get(cmd_idx)
        line_start = len(lines)
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

        elif cmd.command_type == "pause":
            msg = cmd.message.replace("'", "\\'")
            lines.append(f"    protocol.pause('{msg}')")

        elif cmd.command_type == "delay":
            if cmd.minutes:
                lines.append(f"    protocol.delay(minutes={cmd.minutes})")
            elif cmd.seconds:
                lines.append(f"    protocol.delay(seconds={cmd.seconds})")

        elif cmd.command_type == "comment":
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
                kwargs.append(f"mix_before=({cmd.mix_before[0]}, {cmd.mix_before[1]})")
            if cmd.mix_after:
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

        if step_idx is not None:
            for li in range(line_start, len(lines)):
                step_line_map.setdefault(step_idx, []).append(li)

    script = "\n".join(lines)
    if step_summaries:
        return script, step_line_map
    return script
