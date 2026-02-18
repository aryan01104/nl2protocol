import io
from models import ProtocolSchema
from parser import ProtocolParser
from opentrons import simulate


def generate_opentrons_script(protocol: ProtocolSchema) -> str:
    """Generates a valid Opentrons Python script from the Pydantic schema."""
    lines = [
        "from opentrons import protocol_api",
        "",
        f"metadata = {{'protocolName': '{protocol.protocol_name}', 'author': '{protocol.author}', 'apiLevel': '2.15'}}",
        "",
        "def run(protocol: protocol_api.ProtocolContext):",
    ]

    # Load Labware
    labware_map = {}
    for lw in protocol.labware:
        var_name = f"lw_{lw.slot}"
        label_arg = f", label='{lw.label}'" if lw.label else ""
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

        else:
            raise ValueError(f"Unknown command type: {cmd.command_type}")

    return "\n".join(lines)


def verify_protocol(script_code: str) -> tuple[bool, str]:
    """Runs the script through the Opentrons simulator and returns (success, logs/errors)."""
    protocol_file = io.StringIO(script_code)
    try:
        runlog, _ = simulate.simulate(protocol_file)
        # Format the run log into readable output
        log_lines = ["Simulation completed successfully.", "", "Run Log:"]
        for entry in runlog:
            if isinstance(entry, dict):
                msg = entry.get('payload', {}).get('text', str(entry))
                log_lines.append(f"  {msg}")
            else:
                log_lines.append(f"  {entry}")
        return True, "\n".join(log_lines)
    except Exception as e:
        return False, f"Simulation failed: {str(e)}"


class ProtocolAgent:
    def __init__(self, config_path: str = "lab_config.json"):
        self.config_path = config_path
        self.parser = ProtocolParser(config_path=config_path)

    def run_pipeline(self, prompt: str, csv_path: str = None, max_retries: int = 3) -> tuple[str, str] | None:
        """Run the protocol generation pipeline.

        Returns:
            On success: Tuple of (script, simulation_log)
            On failure: None
        """
        print(f"Scientist Intent: {prompt}")

        attempt = 0
        error_log = None

        while attempt < max_retries:
            attempt += 1
            print(f"\n--- Reasoning & Verification: Attempt {attempt} ---")

            # 1. Reasoning: Parser translates Intent -> Pydantic Schema
            protocol_schema = self.parser.parse_intent(prompt, csv_path, error_log)

            if protocol_schema is None:
                print("Failed to parse intent into protocol schema.")
                error_log = "LLM failed to produce valid JSON matching the schema."
                continue

            # 2. Generation: Schema -> Python
            try:
                script = generate_opentrons_script(protocol_schema)
            except ValueError as e:
                print(f"Script generation error: {e}")
                error_log = str(e)
                continue

            # 3. Verification: Simulation
            success, simulation_log = verify_protocol(script)

            if success:
                print("Success! 'Scientist Intent' verified as robotic action.")
                return (script, simulation_log)
            else:
                print(f"Safety Violation Detected: {simulation_log}")
                error_log = simulation_log
                print("Self-Correction Loop: Feeding error back to Reasoning engine...")

        print(f"Failed after {max_retries} attempts.")
        return None


if __name__ == "__main__":
    import sys
    from cli import main

    # If no arguments provided, show help
    if len(sys.argv) == 1:
        sys.argv.append('--help')

    sys.exit(main())
