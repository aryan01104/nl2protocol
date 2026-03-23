"""
Schema Analyzer

Extracts high-level information from protocol schemas:
- Protocol purpose/name
- Equipment summary (labware types, modules)
- Volume ranges used
- Well ranges used
- Time estimates
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Set, Tuple
import re


@dataclass
class LabwareSummary:
    """Summary of a labware item."""
    label: str
    load_name: str
    slot: str
    labware_type: str  # plate, tiprack, reservoir, tuberack, etc.
    well_count: int
    wells_used: Set[str] = field(default_factory=set)

    @property
    def wells_used_str(self) -> str:
        """Format wells used as a readable string."""
        if not self.wells_used:
            return "none"
        wells = sorted(self.wells_used, key=self._well_sort_key)
        return self._format_well_range(wells)

    def _well_sort_key(self, well: str) -> Tuple[int, int]:
        """Sort key for wells (A1, A2, ..., B1, B2, ...)."""
        match = re.match(r'([A-P])(\d+)', well)
        if match:
            return (ord(match.group(1)), int(match.group(2)))
        return (0, 0)

    def _format_well_range(self, wells: List[str]) -> str:
        """Format a list of wells as ranges where possible."""
        if len(wells) <= 3:
            return ", ".join(wells)

        # Try to detect ranges
        # For now, just show first, last, and count
        return f"{wells[0]}-{wells[-1]} ({len(wells)} wells)"


@dataclass
class PipetteSummary:
    """Summary of a pipette."""
    mount: str
    model: str
    min_volume: float
    max_volume: float
    is_multi: bool

    @classmethod
    def from_model(cls, mount: str, model: str) -> "PipetteSummary":
        """Create summary from model name."""
        model_lower = model.lower()

        # Determine volume range
        if "p20" in model_lower:
            min_vol, max_vol = 1, 20
        elif "p300" in model_lower:
            min_vol, max_vol = 20, 300
        elif "p1000" in model_lower:
            min_vol, max_vol = 100, 1000
        elif "p50" in model_lower:
            min_vol, max_vol = 5, 50
        elif "p10" in model_lower:
            min_vol, max_vol = 1, 10
        else:
            min_vol, max_vol = 1, 300  # Default

        is_multi = "multi" in model_lower or "_8_" in model_lower

        return cls(
            mount=mount,
            model=model,
            min_volume=min_vol,
            max_volume=max_vol,
            is_multi=is_multi
        )


@dataclass
class ModuleSummary:
    """Summary of a module."""
    label: str
    module_type: str  # temperature, magnetic, heater_shaker, thermocycler
    slot: str
    temperatures_used: List[float] = field(default_factory=list)
    operations: List[str] = field(default_factory=list)


@dataclass
class SchemaAnalysis:
    """Complete analysis of a protocol schema."""
    protocol_name: str
    author: Optional[str]

    # Equipment
    labware: List[LabwareSummary]
    pipettes: List[PipetteSummary]
    modules: List[ModuleSummary]

    # Statistics
    total_commands: int
    volume_range: Tuple[float, float]  # min, max volumes used
    total_transfer_volume: float
    estimated_duration_minutes: float

    # Inferred purpose
    likely_purpose: str  # "serial dilution", "sample transfer", "PCR setup", etc.
    key_operations: List[str]  # Main operations detected


class SchemaAnalyzer:
    """
    Analyzes a protocol schema to extract high-level information.
    """

    def __init__(self, schema: Dict[str, Any]):
        self.schema = schema
        self.commands = schema.get("commands", [])

    def analyze(self) -> SchemaAnalysis:
        """Perform complete analysis of the schema."""
        labware = self._analyze_labware()
        pipettes = self._analyze_pipettes()
        modules = self._analyze_modules()

        volume_range = self._get_volume_range()
        total_volume = self._get_total_transfer_volume()
        duration = self._estimate_duration()

        purpose = self._infer_purpose()
        key_ops = self._get_key_operations()

        # Update labware with wells used
        self._populate_wells_used(labware)
        # Update modules with operations
        self._populate_module_operations(modules)

        return SchemaAnalysis(
            protocol_name=self.schema.get("protocol_name", "Unknown Protocol"),
            author=self.schema.get("author"),
            labware=labware,
            pipettes=pipettes,
            modules=modules,
            total_commands=len(self.commands),
            volume_range=volume_range,
            total_transfer_volume=total_volume,
            estimated_duration_minutes=duration,
            likely_purpose=purpose,
            key_operations=key_ops
        )

    def _analyze_labware(self) -> List[LabwareSummary]:
        """Analyze labware in the schema."""
        result = []
        for lw in self.schema.get("labware", []):
            load_name = lw.get("load_name", "")
            lw_type = self._infer_labware_type(load_name)
            well_count = self._infer_well_count(load_name)

            result.append(LabwareSummary(
                label=lw.get("label", f"slot_{lw.get('slot')}"),
                load_name=load_name,
                slot=str(lw.get("slot", "")),
                labware_type=lw_type,
                well_count=well_count
            ))
        return result

    def _infer_labware_type(self, load_name: str) -> str:
        """Infer labware type from load_name."""
        load_name = load_name.lower()

        if "tiprack" in load_name:
            return "tiprack"
        elif "reservoir" in load_name:
            return "reservoir"
        elif "tuberack" in load_name:
            return "tuberack"
        elif "plate" in load_name:
            if "deep" in load_name:
                return "deep_well_plate"
            elif "pcr" in load_name:
                return "pcr_plate"
            else:
                return "plate"
        elif "falcon" in load_name or "tube" in load_name:
            return "tube_rack"
        else:
            return "labware"

    def _infer_well_count(self, load_name: str) -> int:
        """Infer well count from load_name."""
        load_name = load_name.lower()

        if "_384_" in load_name:
            return 384
        elif "_96_" in load_name:
            return 96
        elif "_48_" in load_name:
            return 48
        elif "_24_" in load_name:
            return 24
        elif "_12_" in load_name:
            return 12
        elif "_6_" in load_name:
            return 6
        elif "_1_" in load_name:
            return 1
        else:
            return 96  # Default

    def _analyze_pipettes(self) -> List[PipetteSummary]:
        """Analyze pipettes in the schema."""
        result = []
        for pip in self.schema.get("pipettes", []):
            mount = pip.get("mount", "unknown")
            model = pip.get("model", "")
            result.append(PipetteSummary.from_model(mount, model))
        return result

    def _analyze_modules(self) -> List[ModuleSummary]:
        """Analyze modules in the schema."""
        result = []
        for mod in self.schema.get("modules", []):
            result.append(ModuleSummary(
                label=mod.get("label", f"module_{mod.get('slot')}"),
                module_type=mod.get("module_type", "unknown"),
                slot=str(mod.get("slot", ""))
            ))
        return result

    def _safe_float(self, value, default: float = 0.0) -> float:
        """Safely convert a value to float."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _get_volume_range(self) -> Tuple[float, float]:
        """Get min and max volumes used in the protocol."""
        volumes = []
        for cmd in self.commands:
            vol = self._safe_float(cmd.get("volume"))
            if vol > 0:
                volumes.append(vol)

        if volumes:
            return (min(volumes), max(volumes))
        return (0, 0)

    def _get_total_transfer_volume(self) -> float:
        """Estimate total volume transferred."""
        total = 0.0
        for cmd in self.commands:
            cmd_type = cmd.get("command_type", "")
            if cmd_type in ("transfer", "dispense"):
                vol = self._safe_float(cmd.get("volume", 0))
                total += vol
        return total

    def _estimate_duration(self) -> float:
        """Estimate protocol duration in minutes."""
        # Very rough estimates
        duration_seconds = 0.0

        for cmd in self.commands:
            cmd_type = cmd.get("command_type", "")

            # Delays
            if cmd_type == "delay":
                duration_seconds += self._safe_float(cmd.get("seconds", 0))
                duration_seconds += self._safe_float(cmd.get("minutes", 0)) * 60

            # Liquid handling (rough: 3 seconds per command)
            elif cmd_type in ("transfer", "aspirate", "dispense", "mix"):
                duration_seconds += 3

            # Tip operations
            elif cmd_type in ("pick_up_tip", "drop_tip"):
                duration_seconds += 2

            # Module operations (assume waiting)
            elif cmd_type in ("wait_for_temperature",):
                duration_seconds += 60  # 1 min to reach temp

        return duration_seconds / 60

    def _populate_wells_used(self, labware: List[LabwareSummary]):
        """Populate wells_used for each labware."""
        labware_by_label = {lw.label: lw for lw in labware}

        for cmd in self.commands:
            # Check various well fields
            for labware_key in ("labware", "source_labware", "dest_labware"):
                lw_label = cmd.get(labware_key)
                if lw_label and lw_label in labware_by_label:
                    for well_key in ("well", "source_well", "dest_well"):
                        well = cmd.get(well_key)
                        if well:
                            labware_by_label[lw_label].wells_used.add(well)

    def _populate_module_operations(self, modules: List[ModuleSummary]):
        """Populate operations and temperatures for each module."""
        modules_by_label = {m.label: m for m in modules}

        for cmd in self.commands:
            module = cmd.get("module")
            if module and module in modules_by_label:
                mod = modules_by_label[module]
                cmd_type = cmd.get("command_type", "")
                mod.operations.append(cmd_type)

                # Track temperatures
                temp = cmd.get("temperature") or cmd.get("celsius")
                if temp and isinstance(temp, (int, float)):
                    if temp not in mod.temperatures_used:
                        mod.temperatures_used.append(temp)

    def _infer_purpose(self) -> str:
        """Infer the likely purpose of the protocol."""
        name = self.schema.get("protocol_name", "").lower()

        # Check protocol name for hints
        if "serial dilution" in name or "dilution" in name:
            return "serial dilution"
        elif "pcr" in name:
            return "PCR setup"
        elif "elisa" in name:
            return "ELISA"
        elif "extraction" in name:
            return "extraction"
        elif "normalization" in name:
            return "normalization"
        elif "cherry" in name or "picking" in name:
            return "cherry picking"

        # Analyze command patterns
        cmd_types = [c.get("command_type") for c in self.commands]

        # Check for magnetic bead operations
        if any(ct in cmd_types for ct in ("engage_magnets", "engage", "disengage_magnets", "disengage")):
            return "magnetic bead separation"

        # Check for thermocycler
        if any(ct in cmd_types for ct in ("run_profile", "set_lid_temperature")):
            return "thermocycling/PCR"

        # Check for temperature module with delays (incubation)
        if any(ct == "set_temperature" for ct in cmd_types) and any(ct == "delay" for ct in cmd_types):
            return "incubation protocol"

        # Default: sample transfer
        return "liquid transfer protocol"

    def _get_key_operations(self) -> List[str]:
        """Get list of key operations in the protocol."""
        operations = set()

        for cmd in self.commands:
            cmd_type = cmd.get("command_type", "")

            if cmd_type == "transfer":
                operations.add("liquid transfer")
            elif cmd_type == "mix":
                operations.add("mixing")
            elif cmd_type in ("engage_magnets", "engage"):
                operations.add("magnetic separation")
            elif cmd_type == "set_temperature":
                operations.add("temperature control")
            elif cmd_type == "delay":
                operations.add("incubation")
            elif cmd_type in ("run_profile",):
                operations.add("thermocycling")
            elif cmd_type in ("set_shake_speed", "start_shaking"):
                operations.add("shaking")

        return list(operations)
