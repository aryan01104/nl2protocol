"""
NL Generator

Converts detected patterns and schema analysis into precise natural language descriptions.
This is the core rule-based converter - no LLM, purely deterministic.
"""

from typing import List, Dict, Any, Optional
from .pattern_detector import CommandPattern, PatternType
from .schema_analyzer import SchemaAnalysis, LabwareSummary, ModuleSummary
import re


class NLGenerator:
    """
    Generates natural language descriptions from protocol patterns.

    Two output modes:
    1. Precise NL: Detailed, verifiable descriptions (default)
    2. Concise NL: Shorter summaries (optional)
    """

    def __init__(self, analysis: SchemaAnalysis, patterns: List[CommandPattern]):
        self.analysis = analysis
        self.patterns = patterns

        # Build lookup maps
        self.labware_map = {lw.label: lw for lw in analysis.labware}
        self.module_map = {m.label: m for m in analysis.modules}

    def generate_precise_nl(self) -> str:
        """
        Generate a precise, detailed natural language description.

        This description is deterministic and verifiable against the schema.
        """
        parts = []

        # 1. Protocol header (name + purpose)
        header = self._generate_header()
        if header:
            parts.append(header)

        # 2. Equipment summary
        equipment = self._generate_equipment_summary()
        if equipment:
            parts.append(equipment)

        # 3. Step-by-step operations
        steps = self._generate_steps()
        if steps:
            parts.append(steps)

        return " ".join(parts)

    def generate_concise_nl(self) -> str:
        """
        Generate a concise summary suitable for a scientist's quick instruction.

        Focuses on the key operations without exhaustive detail.
        """
        parts = []

        # Get main operations
        main_ops = self._get_main_operations()

        if main_ops:
            parts.append(main_ops)
        else:
            # Fallback: generate from protocol name/purpose
            fallback = self._generate_fallback_concise()
            if fallback:
                parts.append(fallback)

        # Add key parameters
        params = self._get_key_parameters()
        if params:
            parts.append(params)

        result = " ".join(parts)

        # If still empty, use protocol name as last resort
        if not result.strip():
            name = self.analysis.protocol_name
            if name and name.lower() != "unknown protocol":
                result = f"Execute {name} protocol."
            else:
                result = "Execute liquid handling protocol."

        return result

    def _generate_header(self) -> str:
        """Generate protocol header."""
        name = self.analysis.protocol_name
        purpose = self.analysis.likely_purpose

        if name and name.lower() != "unknown protocol":
            return f"{name}:"
        elif purpose:
            return f"{purpose.title()} protocol:"
        return ""

    def _generate_equipment_summary(self) -> str:
        """Generate equipment summary."""
        parts = []

        # Summarize key labware (not tipracks)
        key_labware = [lw for lw in self.analysis.labware if lw.labware_type != "tiprack"]
        if key_labware:
            lw_descriptions = []
            for lw in key_labware[:4]:  # Limit to 4 main items
                lw_descriptions.append(f"{lw.label} ({lw.labware_type}, slot {lw.slot})")
            parts.append(f"Using {', '.join(lw_descriptions)}.")

        # Mention modules if present
        if self.analysis.modules:
            mod_descriptions = []
            for mod in self.analysis.modules:
                mod_descriptions.append(f"{mod.module_type} module")
            parts.append(f"With {', '.join(mod_descriptions)}.")

        return " ".join(parts)

    def _generate_steps(self) -> str:
        """Generate step-by-step description from patterns."""
        steps = []
        step_num = 1

        # Filter out low-level patterns (tip ops, comments)
        significant_patterns = [
            p for p in self.patterns
            if p.pattern_type not in (
                PatternType.TIP_OPERATION,
                PatternType.COMMENT,
                PatternType.UNKNOWN
            )
        ]

        for pattern in significant_patterns:
            description = self._describe_pattern(pattern)
            if description:
                steps.append(f"({step_num}) {description}")
                step_num += 1

        return " ".join(steps)

    def _describe_pattern(self, pattern: CommandPattern) -> str:
        """Generate description for a single pattern."""
        handlers = {
            PatternType.SERIAL_DILUTION: self._describe_serial_dilution,
            PatternType.DISTRIBUTION: self._describe_distribution,
            PatternType.CONSOLIDATION: self._describe_consolidation,
            PatternType.TRANSFER_BATCH: self._describe_batch_transfer,
            PatternType.TRANSFER_SINGLE: self._describe_single_transfer,
            PatternType.MIXING: self._describe_mixing,
            PatternType.MIXING_BATCH: self._describe_batch_mixing,
            PatternType.TEMPERATURE_CONTROL: self._describe_temperature,
            PatternType.MAGNETIC_SEPARATION: self._describe_magnetic,
            PatternType.INCUBATION: self._describe_incubation,
            PatternType.THERMOCYCLER: self._describe_thermocycler,
            PatternType.HEATER_SHAKER: self._describe_heater_shaker,
            PatternType.PAUSE: self._describe_pause,
        }

        handler = handlers.get(pattern.pattern_type)
        if handler:
            return handler(pattern)
        return ""

    def _describe_serial_dilution(self, p: CommandPattern) -> str:
        """Describe serial dilution pattern."""
        dest_wells_clean = [w for w in (p.dest_wells or []) if w]
        wells = self._format_well_range(dest_wells_clean) or "wells"
        volume = self._format_volume(p.volume)
        labware = p.source_labware or "plate"

        desc = f"Perform serial dilution across {wells} in {labware}"
        if volume:
            desc += f", transferring {volume} between wells"
        if p.mix_cycles:
            desc += f", mixing {p.mix_cycles}x after each transfer"
        return desc + "."

    def _describe_distribution(self, p: CommandPattern) -> str:
        """Describe distribution pattern."""
        source_lw = p.source_labware or "source"
        source_well = p.source_wells[0] if p.source_wells and p.source_wells[0] else ""
        source = f"{source_lw} {source_well}".strip() if source_well else source_lw

        dest_wells_clean = [w for w in (p.dest_wells or []) if w]
        dest_wells = self._format_well_range(dest_wells_clean) or "multiple wells"
        dest_labware = p.dest_labware or "destination"
        volume = self._format_volume(p.volume)

        desc = f"Distribute"
        if volume:
            desc += f" {volume}"
        desc += f" from {source} to {dest_labware} wells {dest_wells}"
        return desc + "."

    def _describe_consolidation(self, p: CommandPattern) -> str:
        """Describe consolidation pattern."""
        source_wells_clean = [w for w in (p.source_wells or []) if w]
        source_wells = self._format_well_range(source_wells_clean) or "multiple wells"
        source_labware = p.source_labware or "source"

        dest_lw = p.dest_labware or "destination"
        dest_well = p.dest_wells[0] if p.dest_wells and p.dest_wells[0] else ""
        dest = f"{dest_lw} {dest_well}".strip() if dest_well else dest_lw
        volume = self._format_volume(p.volume)

        desc = f"Consolidate"
        if volume:
            desc += f" {volume}"
        desc += f" from {source_labware} wells {source_wells} into {dest}"
        return desc + "."

    def _describe_batch_transfer(self, p: CommandPattern) -> str:
        """Describe batch transfer pattern."""
        # Filter out None values from wells
        source_wells_clean = [w for w in (p.source_wells or []) if w]
        dest_wells_clean = [w for w in (p.dest_wells or []) if w]

        source_wells = self._format_well_range(source_wells_clean) or "multiple wells"
        dest_wells = self._format_well_range(dest_wells_clean) or "multiple wells"
        volume = self._format_volume(p.volume)

        source_lw = p.source_labware or "source"
        dest_lw = p.dest_labware or "destination"

        # Check if same labware (plate rearrangement) or different (plate-to-plate)
        if source_lw == dest_lw and source_lw != "source":
            desc = f"Transfer"
            if volume:
                desc += f" {volume}"
            desc += f" from wells {source_wells} to {dest_wells} within {source_lw}"
        else:
            desc = f"Transfer"
            if volume:
                desc += f" {volume}"
            desc += f" from {source_lw} ({source_wells}) to {dest_lw} ({dest_wells})"

        return desc + "."

    def _describe_single_transfer(self, p: CommandPattern) -> str:
        """Describe single transfer."""
        source_lw = p.source_labware or "source"
        dest_lw = p.dest_labware or "destination"
        source_well = p.source_wells[0] if p.source_wells and p.source_wells[0] else ""
        dest_well = p.dest_wells[0] if p.dest_wells and p.dest_wells[0] else ""

        source = f"{source_lw} {source_well}".strip() if source_well else source_lw
        dest = f"{dest_lw} {dest_well}".strip() if dest_well else dest_lw
        volume = self._format_volume(p.volume)

        desc = f"Transfer"
        if volume:
            desc += f" {volume}"
        desc += f" from {source} to {dest}"
        return desc + "."

    def _describe_mixing(self, p: CommandPattern) -> str:
        """Describe single mixing operation."""
        location = ""
        source_well = p.source_wells[0] if p.source_wells and p.source_wells[0] else ""
        if p.source_labware and source_well:
            location = f" in {p.source_labware} {source_well}"
        elif p.source_labware:
            location = f" in {p.source_labware}"

        cycles = p.mix_cycles or 3
        volume = self._format_volume(p.mix_volume)

        desc = f"Mix {cycles} times"
        if volume:
            desc += f" at {volume}"
        desc += location
        return desc + "."

    def _describe_batch_mixing(self, p: CommandPattern) -> str:
        """Describe batch mixing operation."""
        wells_clean = [w for w in (p.source_wells or []) if w]
        wells = self._format_well_range(wells_clean) or "multiple wells"
        labware = p.source_labware or "plate"
        cycles = p.mix_cycles or 3
        volume = self._format_volume(p.mix_volume)

        desc = f"Mix wells {wells} in {labware}, {cycles} cycles"
        if volume:
            desc += f" at {volume}"
        return desc + "."

    def _describe_temperature(self, p: CommandPattern) -> str:
        """Describe temperature control operation."""
        cmd = p.commands[0] if p.commands else {}
        cmd_type = cmd.get("command_type", "")

        if cmd_type == "deactivate_temperature":
            return "Deactivate temperature control."

        if p.temperature:
            if "wait" in cmd_type:
                return f"Wait for temperature to reach {p.temperature}°C."
            else:
                return f"Set temperature to {p.temperature}°C."
        return "Adjust temperature."

    def _describe_magnetic(self, p: CommandPattern) -> str:
        """Describe magnetic module operation."""
        cmd = p.commands[0] if p.commands else {}
        cmd_type = cmd.get("command_type", "")

        if "disengage" in cmd_type:
            return "Disengage magnets to release beads."
        else:
            if p.magnet_height:
                return f"Engage magnets at {p.magnet_height}mm height for bead separation."
            return "Engage magnets for bead separation."

    def _describe_incubation(self, p: CommandPattern) -> str:
        """Describe incubation/delay operation."""
        duration = self._safe_float(p.duration_seconds)
        if duration > 0:
            if duration >= 3600:
                hours = duration / 3600
                return f"Incubate for {hours:.1f} hours."
            elif duration >= 60:
                minutes = duration / 60
                return f"Incubate for {minutes:.0f} minutes."
            else:
                return f"Wait {duration:.0f} seconds."
        return "Pause for incubation."

    def _describe_thermocycler(self, p: CommandPattern) -> str:
        """Describe thermocycler operation."""
        cmd = p.commands[0] if p.commands else {}
        cmd_type = cmd.get("command_type", "")

        if cmd_type == "run_profile":
            if p.cycles:
                return f"Run thermocycler profile for {p.cycles} cycles."
            return "Run thermocycler profile."
        elif "lid" in cmd_type:
            if p.temperature:
                return f"Set thermocycler lid to {p.temperature}°C."
            elif "open" in cmd_type:
                return "Open thermocycler lid."
            elif "close" in cmd_type:
                return "Close thermocycler lid."
        return "Operate thermocycler."

    def _describe_heater_shaker(self, p: CommandPattern) -> str:
        """Describe heater-shaker operation."""
        cmd = p.commands[0] if p.commands else {}
        cmd_type = cmd.get("command_type", "")

        if "latch" in cmd_type:
            if "open" in cmd_type:
                return "Open heater-shaker latch."
            else:
                return "Close heater-shaker latch."
        elif p.shake_speed:
            return f"Shake at {p.shake_speed} RPM."
        elif "stop" in cmd_type:
            return "Stop shaking."
        return "Operate heater-shaker."

    def _describe_pause(self, p: CommandPattern) -> str:
        """Describe pause operation."""
        cmd = p.commands[0] if p.commands else {}
        message = cmd.get("message", "")

        if message:
            return f"Pause: {message}"
        return "Pause for manual intervention."

    def _generate_fallback_concise(self) -> str:
        """Generate fallback concise description when main operations are empty."""
        name = self.analysis.protocol_name
        purpose = self.analysis.likely_purpose

        # Try to use purpose
        if purpose and purpose != "liquid transfer protocol":
            return f"Perform {purpose}."

        # Try to extract action from protocol name
        if name and name.lower() != "unknown protocol":
            name_lower = name.lower()
            if "dilution" in name_lower:
                return "Perform serial dilution."
            elif "pcr" in name_lower:
                return "Set up PCR reactions."
            elif "transfer" in name_lower:
                return "Transfer samples between plates."
            elif "mix" in name_lower:
                return "Mix samples."
            elif "fill" in name_lower or "distribute" in name_lower:
                return "Distribute reagents to plate."

        # Count total commands to give some context
        total_cmds = self.analysis.total_commands
        if total_cmds > 0:
            return f"Execute {total_cmds}-step liquid handling protocol."

        return ""

    def _get_main_operations(self) -> str:
        """Get concise description of main operations."""
        # Count pattern types
        pattern_counts = {}
        for p in self.patterns:
            if p.pattern_type not in (PatternType.TIP_OPERATION, PatternType.COMMENT, PatternType.UNKNOWN):
                pattern_counts[p.pattern_type] = pattern_counts.get(p.pattern_type, 0) + 1

        if not pattern_counts:
            return ""

        # Build description based on dominant patterns
        descriptions = []

        # Check for key patterns
        if PatternType.SERIAL_DILUTION in pattern_counts:
            # Find the serial dilution pattern for details
            for p in self.patterns:
                if p.pattern_type == PatternType.SERIAL_DILUTION:
                    wells = self._format_well_range(p.dest_wells)
                    descriptions.append(f"serial dilution across {wells}")
                    break

        if PatternType.DISTRIBUTION in pattern_counts:
            count = pattern_counts[PatternType.DISTRIBUTION]
            if count > 1:
                descriptions.append(f"{count} distribution steps")
            else:
                for p in self.patterns:
                    if p.pattern_type == PatternType.DISTRIBUTION:
                        dest_wells = self._format_well_range(p.dest_wells)
                        descriptions.append(f"distribute to {dest_wells}")
                        break

        if PatternType.CONSOLIDATION in pattern_counts:
            descriptions.append("sample consolidation")

        if PatternType.MAGNETIC_SEPARATION in pattern_counts:
            descriptions.append("magnetic bead separation")

        if PatternType.TEMPERATURE_CONTROL in pattern_counts:
            # Get temperatures used
            temps = set()
            for p in self.patterns:
                if p.pattern_type == PatternType.TEMPERATURE_CONTROL and p.temperature:
                    temps.add(p.temperature)
            if temps:
                descriptions.append(f"temperature control ({', '.join(f'{t}°C' for t in sorted(temps))})")

        if PatternType.THERMOCYCLER in pattern_counts:
            descriptions.append("thermocycling")

        # Count transfers
        transfer_patterns = [PatternType.TRANSFER_BATCH, PatternType.TRANSFER_SINGLE]
        transfer_count = sum(pattern_counts.get(t, 0) for t in transfer_patterns)
        if transfer_count > 3 and not descriptions:
            descriptions.append(f"{transfer_count} liquid transfers")

        if descriptions:
            return f"Perform {', '.join(descriptions)}."
        return ""

    def _get_key_parameters(self) -> str:
        """Get key parameters (volumes, wells, etc.)."""
        params = []

        # Volume range
        vol_min, vol_max = self.analysis.volume_range
        vol_min = self._safe_float(vol_min)
        vol_max = self._safe_float(vol_max)

        if vol_min > 0 and vol_max > 0:
            if vol_min == vol_max:
                params.append(f"using {self._format_volume(vol_min)}")
            else:
                params.append(f"volumes {self._format_volume(vol_min)}-{self._format_volume(vol_max)}")

        # Duration if significant
        duration = self._safe_float(self.analysis.estimated_duration_minutes)
        if duration > 5:
            params.append(f"~{duration:.0f} min total")

        if params:
            return f"({', '.join(params)})"
        return ""

    def _safe_float(self, value, default: float = 0.0) -> float:
        """Safely convert a value to float."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _format_volume(self, volume) -> str:
        """Format volume with appropriate units."""
        vol = self._safe_float(volume)
        if vol <= 0:
            return ""
        if vol >= 1000:
            return f"{vol/1000:.1f}mL"
        elif vol >= 1:
            return f"{vol:.0f}µL"
        else:
            return f"{vol:.2f}µL"

    def _format_well_range(self, wells: List[str]) -> str:
        """Format a list of wells as a concise range."""
        # Filter out None and empty values
        wells = [w for w in (wells or []) if w]

        if not wells:
            return ""
        if len(wells) == 1:
            return wells[0]
        if len(wells) <= 3:
            return ", ".join(wells)

        # Sort wells
        sorted_wells = sorted(wells, key=self._well_sort_key)

        # Try to detect patterns (row, column, or range)
        first, last = sorted_wells[0], sorted_wells[-1]

        # Check if it's a full row
        if self._is_row_range(sorted_wells):
            row = first[0]
            first_col = re.search(r'\d+', first).group()
            last_col = re.search(r'\d+', last).group()
            return f"row {row} ({first}-{last})"

        # Check if it's a full column
        if self._is_column_range(sorted_wells):
            col = re.search(r'\d+', first).group()
            first_row = first[0]
            last_row = last[0]
            return f"column {col} ({first_row}-{last_row})"

        # Default: show range with count
        return f"{first}-{last} ({len(wells)} wells)"

    def _well_sort_key(self, well: str):
        """Sort key for wells."""
        match = re.match(r'([A-P])(\d+)', well)
        if match:
            return (ord(match.group(1)), int(match.group(2)))
        return (0, 0)

    def _is_row_range(self, wells: List[str]) -> bool:
        """Check if wells form a row range (same row, sequential columns)."""
        if not wells:
            return False

        rows = set()
        cols = []
        for well in wells:
            match = re.match(r'([A-P])(\d+)', well)
            if match:
                rows.add(match.group(1))
                cols.append(int(match.group(2)))

        if len(rows) != 1:
            return False

        cols.sort()
        return cols == list(range(cols[0], cols[-1] + 1))

    def _is_column_range(self, wells: List[str]) -> bool:
        """Check if wells form a column range (same column, sequential rows)."""
        if not wells:
            return False

        cols = set()
        rows = []
        for well in wells:
            match = re.match(r'([A-P])(\d+)', well)
            if match:
                rows.append(ord(match.group(1)) - ord('A'))
                cols.add(int(match.group(2)))

        if len(cols) != 1:
            return False

        rows.sort()
        return rows == list(range(rows[0], rows[-1] + 1))
