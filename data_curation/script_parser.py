"""
script_parser.py

Parse Opentrons Python protocol scripts to extract schema components.
Extracts: labware, pipettes, and commands.

Usage:
    from script_parser import parse_protocol_script
    schema = parse_protocol_script("path/to/protocol.py")
"""

import ast
import re
from typing import Dict, List, Any, Optional
from pathlib import Path


def parse_protocol_script(script_path: str) -> Optional[Dict[str, Any]]:
    """
    Parse an Opentrons protocol script and extract schema components.

    Args:
        script_path: Path to the Python protocol file

    Returns:
        Dict with labware, pipettes, commands, and metadata
        None if parsing fails
    """
    try:
        with open(script_path, 'r') as f:
            source = f.read()
        return parse_protocol_source(source, script_path)
    except Exception as e:
        print(f"Error reading {script_path}: {e}")
        return None


def parse_protocol_source(source: str, source_name: str = "<string>") -> Optional[Dict[str, Any]]:
    """
    Parse protocol source code and extract schema.

    Args:
        source: Python source code
        source_name: Name for error messages

    Returns:
        Extracted schema dict or None
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        print(f"Syntax error in {source_name}: {e}")
        return None

    extractor = ProtocolExtractor()
    extractor.visit(tree)

    # Also extract metadata from source
    metadata = extract_metadata(source)

    return {
        "protocol_name": metadata.get("protocolName", Path(source_name).stem),
        "author": metadata.get("author", "Unknown"),
        "labware": extractor.labware,
        "pipettes": extractor.pipettes,
        "modules": extractor.modules,
        "commands": extractor.commands,
        "raw_script": source
    }


def extract_metadata(source: str) -> Dict[str, str]:
    """Extract metadata dict from source code."""
    metadata = {}

    # Match metadata = {...} or metadata = dict(...)
    # Simple regex approach for common patterns
    patterns = [
        r"['\"]protocolName['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"['\"]author['\"]\s*:\s*['\"]([^'\"]+)['\"]",
        r"['\"]apiLevel['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    ]
    keys = ["protocolName", "author", "apiLevel"]

    for pattern, key in zip(patterns, keys):
        match = re.search(pattern, source)
        if match:
            metadata[key] = match.group(1)

    return metadata


class ProtocolExtractor(ast.NodeVisitor):
    """AST visitor that extracts protocol components."""

    def __init__(self):
        self.labware: List[Dict] = []
        self.pipettes: List[Dict] = []
        self.modules: List[Dict] = []
        self.commands: List[Dict] = []

        # Track variable assignments for reference resolution
        self.var_to_labware: Dict[str, Dict] = {}
        self.var_to_pipette: Dict[str, str] = {}  # var -> mount
        self.var_to_module: Dict[str, Dict] = {}  # var -> module info

        # Slot counter for labware without explicit slots
        self.slot_counter = 1

    def visit_Assign(self, node: ast.Assign):
        """Handle variable assignments."""
        if len(node.targets) != 1:
            self.generic_visit(node)
            return

        target = node.targets[0]
        if not isinstance(target, ast.Name):
            self.generic_visit(node)
            return

        var_name = target.id

        # Check if this is a method call
        if isinstance(node.value, ast.Call):
            self._handle_call(node.value, var_name)

        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr):
        """Handle expression statements (method calls without assignment)."""
        if isinstance(node.value, ast.Call):
            self._handle_call(node.value, None)
        self.generic_visit(node)

    def _handle_call(self, call: ast.Call, assigned_var: Optional[str]):
        """Process a function/method call."""
        func_name = self._get_call_name(call)

        if func_name == "load_labware":
            self._handle_load_labware(call, assigned_var)
        elif func_name == "load_instrument":
            self._handle_load_instrument(call, assigned_var)
        elif func_name == "load_module":
            self._handle_load_module(call, assigned_var)
        elif func_name in ("aspirate", "dispense", "mix", "blow_out", "touch_tip",
                          "air_gap", "pick_up_tip", "drop_tip", "return_tip",
                          "transfer", "distribute", "consolidate"):
            self._handle_command(call, func_name)
        elif func_name in ("pause", "delay", "comment"):
            self._handle_flow_control(call, func_name)
        elif func_name in ("set_temperature", "await_temperature", "deactivate",
                          "engage", "disengage", "set_and_wait_for_shake_speed",
                          "stop_shaking", "open_labware_latch", "close_labware_latch",
                          "open_lid", "close_lid", "set_block_temperature",
                          "set_lid_temperature", "execute_profile"):
            self._handle_module_command(call, func_name)

    def _get_call_name(self, call: ast.Call) -> Optional[str]:
        """Extract the method/function name from a call."""
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        elif isinstance(call.func, ast.Name):
            return call.func.id
        return None

    def _get_caller_var(self, call: ast.Call) -> Optional[str]:
        """Get the variable name that the method is called on."""
        if isinstance(call.func, ast.Attribute):
            if isinstance(call.func.value, ast.Name):
                return call.func.value.id
        return None

    def _handle_load_labware(self, call: ast.Call, var_name: Optional[str]):
        """Extract labware from load_labware call."""
        args = self._extract_args(call, ["load_name", "location", "label"])

        load_name = args.get("load_name", "")
        location = args.get("location", str(self.slot_counter))
        label = args.get("label", var_name)

        # Normalize slot
        slot = str(location)

        labware = {
            "slot": slot,
            "load_name": load_name,
            "label": label
        }

        self.labware.append(labware)

        if var_name:
            self.var_to_labware[var_name] = labware

        self.slot_counter += 1

    def _handle_load_instrument(self, call: ast.Call, var_name: Optional[str]):
        """Extract pipette from load_instrument call."""
        args = self._extract_args(call, ["instrument_name", "mount", "tip_racks"])

        model = args.get("instrument_name", "")
        mount = args.get("mount", "left")

        # Extract tiprack references
        tipracks = []
        tip_racks_arg = args.get("tip_racks")
        if tip_racks_arg and isinstance(tip_racks_arg, list):
            for tr in tip_racks_arg:
                if tr in self.var_to_labware:
                    tipracks.append(self.var_to_labware[tr].get("label", tr))

        pipette = {
            "mount": mount,
            "model": model,
            "tipracks": tipracks
        }

        self.pipettes.append(pipette)

        if var_name:
            self.var_to_pipette[var_name] = mount

    def _handle_command(self, call: ast.Call, cmd_type: str):
        """Extract liquid handling commands."""
        caller = self._get_caller_var(call)
        mount = self.var_to_pipette.get(caller, "left")

        cmd = {"command_type": cmd_type, "pipette": mount}

        if cmd_type == "aspirate":
            args = self._extract_args(call, ["volume", "location"])
            cmd["volume"] = args.get("volume", 0)
            loc = self._parse_location(args.get("location"))
            cmd.update(loc)

        elif cmd_type == "dispense":
            args = self._extract_args(call, ["volume", "location"])
            cmd["volume"] = args.get("volume", 0)
            loc = self._parse_location(args.get("location"))
            cmd.update(loc)

        elif cmd_type == "mix":
            args = self._extract_args(call, ["repetitions", "volume", "location"])
            cmd["repetitions"] = args.get("repetitions", 3)
            cmd["volume"] = args.get("volume", 0)
            loc = self._parse_location(args.get("location"))
            cmd.update(loc)

        elif cmd_type == "transfer":
            args = self._extract_args(call, ["volume", "source", "dest", "new_tip", "mix_before", "mix_after"])
            cmd["volume"] = args.get("volume", 0)
            src = self._parse_location(args.get("source"))
            dst = self._parse_location(args.get("dest"))
            cmd["source_labware"] = src.get("labware", "")
            cmd["source_well"] = src.get("well", "A1")
            cmd["dest_labware"] = dst.get("labware", "")
            cmd["dest_well"] = dst.get("well", "A1")
            cmd["new_tip"] = args.get("new_tip", "always")
            if args.get("mix_before"):
                cmd["mix_before"] = args["mix_before"]
            if args.get("mix_after"):
                cmd["mix_after"] = args["mix_after"]

        elif cmd_type == "distribute":
            args = self._extract_args(call, ["volume", "source", "dest", "new_tip"])
            cmd["volume"] = args.get("volume", 0)
            src = self._parse_location(args.get("source"))
            cmd["source_labware"] = src.get("labware", "")
            cmd["source_well"] = src.get("well", "A1")
            # dest could be a list
            cmd["dest_labware"] = ""
            cmd["dest_wells"] = []
            cmd["new_tip"] = args.get("new_tip", "once")

        elif cmd_type == "consolidate":
            args = self._extract_args(call, ["volume", "source", "dest", "new_tip"])
            cmd["volume"] = args.get("volume", 0)
            cmd["source_labware"] = ""
            cmd["source_wells"] = []
            dst = self._parse_location(args.get("dest"))
            cmd["dest_labware"] = dst.get("labware", "")
            cmd["dest_well"] = dst.get("well", "A1")
            cmd["new_tip"] = args.get("new_tip", "once")

        elif cmd_type in ("blow_out", "touch_tip"):
            args = self._extract_args(call, ["location"])
            loc = self._parse_location(args.get("location"))
            if loc.get("labware"):
                cmd.update(loc)

        elif cmd_type == "air_gap":
            args = self._extract_args(call, ["volume"])
            cmd["volume"] = args.get("volume", 0)

        elif cmd_type in ("pick_up_tip", "drop_tip"):
            args = self._extract_args(call, ["location"])
            loc = self._parse_location(args.get("location"))
            if loc.get("labware"):
                cmd.update(loc)

        elif cmd_type == "return_tip":
            pass  # No additional args

        self.commands.append(cmd)

    def _handle_load_module(self, call: ast.Call, var_name: Optional[str]):
        """Extract module from load_module call."""
        args = self._extract_args(call, ["module_name", "location"])

        module_name = args.get("module_name", "")
        location = args.get("location", str(self.slot_counter))

        # Map API module names to our types
        module_type_map = {
            "temperature module": "temperature",
            "temperature module gen2": "temperature",
            "tempdeck": "temperature",
            "magnetic module": "magnetic",
            "magnetic module gen2": "magnetic",
            "magdeck": "magnetic",
            "heaterShakerModuleV1": "heater_shaker",
            "heater-shaker module": "heater_shaker",
            "thermocyclerModuleV1": "thermocycler",
            "thermocyclerModuleV2": "thermocycler",
            "thermocycler module": "thermocycler",
            "thermocycler module gen2": "thermocycler",
        }

        module_type = module_type_map.get(module_name, module_name)
        slot = str(location)

        module = {
            "module_type": module_type,
            "slot": slot,
            "label": var_name
        }

        self.modules.append(module)

        if var_name:
            self.var_to_module[var_name] = module

        self.slot_counter += 1

    def _handle_module_command(self, call: ast.Call, cmd_type: str):
        """Extract module-related commands."""
        caller = self._get_caller_var(call)
        module_info = self.var_to_module.get(caller, {})
        module_ref = module_info.get("label", module_info.get("slot", caller))

        cmd = {"command_type": cmd_type, "module": module_ref}

        if cmd_type == "set_temperature":
            args = self._extract_args(call, ["celsius"])
            cmd["celsius"] = args.get("celsius", args.get("temperature", 0))

        elif cmd_type == "await_temperature":
            args = self._extract_args(call, ["celsius"])
            cmd["command_type"] = "wait_for_temperature"
            if args.get("celsius"):
                cmd["celsius"] = args["celsius"]

        elif cmd_type == "deactivate":
            cmd["command_type"] = "deactivate"

        elif cmd_type == "engage":
            args = self._extract_args(call, ["height", "height_from_base", "offset"])
            cmd["command_type"] = "engage_magnets"
            height = args.get("height_from_base") or args.get("height") or args.get("offset")
            if height:
                cmd["height"] = height

        elif cmd_type == "disengage":
            cmd["command_type"] = "disengage_magnets"

        elif cmd_type == "set_and_wait_for_shake_speed":
            args = self._extract_args(call, ["rpm"])
            cmd["command_type"] = "set_shake_speed"
            cmd["rpm"] = args.get("rpm", 0)

        elif cmd_type == "stop_shaking":
            cmd["command_type"] = "set_shake_speed"
            cmd["rpm"] = 0

        elif cmd_type == "open_labware_latch":
            cmd["command_type"] = "open_latch"

        elif cmd_type == "close_labware_latch":
            cmd["command_type"] = "close_latch"

        elif cmd_type == "open_lid":
            cmd["command_type"] = "open_lid"

        elif cmd_type == "close_lid":
            cmd["command_type"] = "close_lid"

        elif cmd_type == "set_block_temperature":
            args = self._extract_args(call, ["temperature", "hold_time_seconds", "hold_time_minutes", "block_max_volume"])
            cmd["celsius"] = args.get("temperature", 0)
            if args.get("hold_time_seconds"):
                cmd["hold_time_seconds"] = args["hold_time_seconds"]
            if args.get("hold_time_minutes"):
                cmd["hold_time_minutes"] = args["hold_time_minutes"]

        elif cmd_type == "set_lid_temperature":
            args = self._extract_args(call, ["temperature"])
            cmd["celsius"] = args.get("temperature", 0)

        elif cmd_type == "execute_profile":
            args = self._extract_args(call, ["steps", "repetitions", "block_max_volume"])
            cmd["command_type"] = "run_profile"
            cmd["steps"] = args.get("steps", [])
            cmd["repetitions"] = args.get("repetitions", 1)

        self.commands.append(cmd)

    def _handle_flow_control(self, call: ast.Call, cmd_type: str):
        """Extract flow control commands."""
        caller = self._get_caller_var(call)
        mount = self.var_to_pipette.get(caller, "left")

        cmd = {"command_type": cmd_type, "pipette": mount}

        if cmd_type == "pause":
            args = self._extract_args(call, ["msg"])
            cmd["message"] = args.get("msg", "Paused")

        elif cmd_type == "delay":
            args = self._extract_args(call, ["seconds", "minutes"])
            if args.get("seconds"):
                cmd["seconds"] = args["seconds"]
            if args.get("minutes"):
                cmd["minutes"] = args["minutes"]

        elif cmd_type == "comment":
            args = self._extract_args(call, ["msg"])
            cmd["message"] = args.get("msg", "")

        self.commands.append(cmd)

    def _extract_args(self, call: ast.Call, param_names: List[str]) -> Dict[str, Any]:
        """Extract positional and keyword arguments from a call."""
        result = {}

        # Positional args
        for i, arg in enumerate(call.args):
            if i < len(param_names):
                result[param_names[i]] = self._eval_node(arg)

        # Keyword args
        for kw in call.keywords:
            if kw.arg:
                result[kw.arg] = self._eval_node(kw.value)

        return result

    def _eval_node(self, node: ast.AST) -> Any:
        """Evaluate an AST node to get its value."""
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Str):  # Python 3.7 compat
            return node.s
        elif isinstance(node, ast.Num):  # Python 3.7 compat
            return node.n
        elif isinstance(node, ast.Name):
            return node.id  # Return variable name
        elif isinstance(node, ast.List):
            return [self._eval_node(elt) for elt in node.elts]
        elif isinstance(node, ast.Tuple):
            return tuple(self._eval_node(elt) for elt in node.elts)
        elif isinstance(node, ast.Subscript):
            # Handle labware['A1'] style
            return self._eval_subscript(node)
        elif isinstance(node, ast.Call):
            # Return the call name for reference
            return self._get_call_name(node)
        return None

    def _eval_subscript(self, node: ast.Subscript) -> str:
        """Evaluate subscript like plate['A1'] -> 'plate:A1'."""
        if isinstance(node.value, ast.Name):
            var = node.value.id
            if isinstance(node.slice, ast.Constant):
                well = node.slice.value
            elif isinstance(node.slice, ast.Str):
                well = node.slice.s
            elif isinstance(node.slice, ast.Index):  # Python 3.8 compat
                well = self._eval_node(node.slice.value)
            else:
                well = "A1"
            return f"{var}:{well}"
        return ""

    def _parse_location(self, location: Any) -> Dict[str, str]:
        """Parse a location string like 'plate:A1' into labware and well."""
        if not location:
            return {}

        if isinstance(location, str) and ":" in location:
            var, well = location.split(":", 1)
            # Resolve variable to labware label
            if var in self.var_to_labware:
                labware = self.var_to_labware[var].get("label", var)
            else:
                labware = var
            return {"labware": labware, "well": well}

        return {}


if __name__ == "__main__":
    import sys
    import json

    if len(sys.argv) < 2:
        print("Usage: python script_parser.py <protocol.py>")
        sys.exit(1)

    result = parse_protocol_script(sys.argv[1])
    if result:
        # Remove raw_script for display
        display = {k: v for k, v in result.items() if k != "raw_script"}
        print(json.dumps(display, indent=2))
    else:
        print("Failed to parse protocol")
        sys.exit(1)
