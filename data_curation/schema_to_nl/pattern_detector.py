"""
Pattern Detector

Analyzes command sequences to identify high-level patterns:
- Serial dilutions
- Distributions (one-to-many)
- Consolidations (many-to-one)
- Plate-to-plate transfers
- Mixing operations
- Module operations (temperature, magnetic, etc.)
- Incubation/delay sequences
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
from enum import Enum
import re


class PatternType(Enum):
    """Types of patterns we can detect in command sequences."""
    TRANSFER_SINGLE = "transfer_single"
    TRANSFER_BATCH = "transfer_batch"
    DISTRIBUTION = "distribution"  # One source to many destinations
    CONSOLIDATION = "consolidation"  # Many sources to one destination
    SERIAL_DILUTION = "serial_dilution"
    MIXING = "mixing"
    MIXING_BATCH = "mixing_batch"
    TEMPERATURE_CONTROL = "temperature_control"
    MAGNETIC_SEPARATION = "magnetic_separation"
    INCUBATION = "incubation"
    THERMOCYCLER = "thermocycler"
    HEATER_SHAKER = "heater_shaker"
    TIP_OPERATION = "tip_operation"
    PAUSE = "pause"
    COMMENT = "comment"
    UNKNOWN = "unknown"


@dataclass
class CommandPattern:
    """A detected pattern with all relevant details."""
    pattern_type: PatternType
    commands: List[Dict]  # The raw commands in this pattern

    # Transfer details
    source_labware: Optional[str] = None
    source_wells: List[str] = field(default_factory=list)
    dest_labware: Optional[str] = None
    dest_wells: List[str] = field(default_factory=list)
    volume: Optional[float] = None
    volumes: List[float] = field(default_factory=list)  # For varying volumes

    # Pipette info
    pipette: Optional[str] = None

    # Mixing details
    mix_cycles: Optional[int] = None
    mix_volume: Optional[float] = None

    # Module details
    module: Optional[str] = None
    module_type: Optional[str] = None
    temperature: Optional[float] = None
    duration_seconds: Optional[float] = None
    shake_speed: Optional[int] = None
    magnet_height: Optional[float] = None

    # Thermocycler
    cycles: Optional[int] = None
    steps: List[Dict] = field(default_factory=list)

    # Metadata
    repetitions: int = 1
    new_tip_per_transfer: bool = False

    def __post_init__(self):
        if self.volumes and not self.volume:
            # Set volume to most common or first
            self.volume = self.volumes[0] if self.volumes else None


class PatternDetector:
    """
    Detects high-level patterns in protocol command sequences.

    Analyzes sequences of low-level commands (aspirate, dispense, etc.)
    and groups them into meaningful patterns (serial dilution, distribution, etc.)
    """

    def __init__(self, schema: Dict[str, Any]):
        self.schema = schema
        self.commands = schema.get("commands", [])
        self.labware = self._index_labware(schema.get("labware", []))
        self.pipettes = self._index_pipettes(schema.get("pipettes", []))
        self.modules = self._index_modules(schema.get("modules", []))

    def _safe_float(self, value, default: float = None) -> Optional[float]:
        """Safely convert a value to float."""
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _safe_int(self, value, default: int = None) -> Optional[int]:
        """Safely convert a value to int."""
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _index_labware(self, labware_list: List[Dict]) -> Dict[str, Dict]:
        """Create lookup dict for labware by label."""
        result = {}
        for lw in labware_list:
            label = lw.get("label", f"slot_{lw.get('slot', 'unknown')}")
            result[label] = lw
        return result

    def _index_pipettes(self, pipette_list: List[Dict]) -> Dict[str, Dict]:
        """Create lookup dict for pipettes by mount."""
        result = {}
        for pip in pipette_list:
            mount = pip.get("mount", "unknown")
            result[mount] = pip
        return result

    def _index_modules(self, module_list: List[Dict]) -> Dict[str, Dict]:
        """Create lookup dict for modules by label."""
        result = {}
        for mod in module_list:
            label = mod.get("label", f"module_slot_{mod.get('slot', 'unknown')}")
            result[label] = mod
        return result

    def detect_patterns(self) -> List[CommandPattern]:
        """
        Analyze all commands and return detected patterns.

        Returns:
            List of CommandPattern objects representing high-level operations.
        """
        patterns = []
        i = 0

        while i < len(self.commands):
            cmd = self.commands[i]
            cmd_type = cmd.get("command_type", "")

            # Try to match patterns in order of specificity
            pattern, consumed = self._try_detect_pattern(i)

            if pattern:
                patterns.append(pattern)
                i += consumed
            else:
                # Single command, wrap as unknown pattern
                patterns.append(CommandPattern(
                    pattern_type=PatternType.UNKNOWN,
                    commands=[cmd]
                ))
                i += 1

        # Post-process: merge adjacent similar patterns
        patterns = self._merge_adjacent_patterns(patterns)

        return patterns

    def _try_detect_pattern(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Try to detect a pattern starting at the given index.

        Returns:
            (pattern, num_commands_consumed) or (None, 0)
        """
        # Try patterns in order of specificity (most specific first)
        detectors = [
            self._detect_serial_dilution,
            self._detect_distribution,
            self._detect_consolidation,
            self._detect_batch_transfer,
            self._detect_batch_mixing,
            self._detect_thermocycler_operation,
            self._detect_heater_shaker_operation,
            self._detect_magnetic_operation,
            self._detect_temperature_operation,
            self._detect_incubation,
            self._detect_single_transfer,
            self._detect_single_mixing,
            self._detect_tip_operation,
            self._detect_pause_comment,
        ]

        for detector in detectors:
            pattern, consumed = detector(start_idx)
            if pattern:
                return pattern, consumed

        return None, 0

    def _detect_serial_dilution(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Detect serial dilution pattern:
        - Transfer from well N to well N+1
        - Mix at destination
        - Repeat sequentially across row or column
        """
        if start_idx >= len(self.commands):
            return None, 0

        commands = []
        wells_sequence = []
        volume = None
        mix_info = None
        source_labware = None
        pipette = None

        i = start_idx
        while i < len(self.commands):
            cmd = self.commands[i]
            cmd_type = cmd.get("command_type", "")

            # Look for transfer or aspirate/dispense pairs
            if cmd_type == "transfer":
                src_well = cmd.get("source_well")
                dst_well = cmd.get("dest_well")

                if wells_sequence:
                    # Check if this continues the sequence
                    last_dst = wells_sequence[-1][1]
                    if src_well != last_dst:
                        break  # Sequence broken

                wells_sequence.append((src_well, dst_well))
                volume = cmd.get("volume", volume)
                source_labware = source_labware or cmd.get("source_labware")
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1

                # Check for mix after transfer
                if i < len(self.commands):
                    next_cmd = self.commands[i]
                    if next_cmd.get("command_type") == "mix":
                        mix_info = (next_cmd.get("repetitions", 3), next_cmd.get("volume"))
                        commands.append(next_cmd)
                        i += 1

            elif cmd_type in ("aspirate", "dispense"):
                # Try to pair aspirate/dispense as transfer
                if cmd_type == "aspirate" and i + 1 < len(self.commands):
                    next_cmd = self.commands[i + 1]
                    if next_cmd.get("command_type") == "dispense":
                        src_well = cmd.get("well")
                        dst_well = next_cmd.get("well")

                        if wells_sequence:
                            last_dst = wells_sequence[-1][1]
                            if src_well != last_dst:
                                break

                        wells_sequence.append((src_well, dst_well))
                        volume = cmd.get("volume", volume)
                        source_labware = source_labware or cmd.get("labware")
                        pipette = pipette or cmd.get("pipette")
                        commands.extend([cmd, next_cmd])
                        i += 2

                        # Check for mix
                        if i < len(self.commands):
                            mix_cmd = self.commands[i]
                            if mix_cmd.get("command_type") == "mix":
                                mix_info = (mix_cmd.get("repetitions", 3), mix_cmd.get("volume"))
                                commands.append(mix_cmd)
                                i += 1
                        continue
                break
            else:
                break

        # Need at least 3 sequential transfers for serial dilution
        if len(wells_sequence) >= 3 and self._is_sequential_wells(wells_sequence):
            all_wells = [wells_sequence[0][0]] + [w[1] for w in wells_sequence]

            return CommandPattern(
                pattern_type=PatternType.SERIAL_DILUTION,
                commands=commands,
                source_labware=source_labware,
                source_wells=[wells_sequence[0][0]],
                dest_labware=source_labware,  # Same plate for serial dilution
                dest_wells=all_wells,
                volume=volume,
                pipette=pipette,
                mix_cycles=mix_info[0] if mix_info else None,
                mix_volume=mix_info[1] if mix_info else None,
                repetitions=len(wells_sequence)
            ), len(commands)

        return None, 0

    def _is_sequential_wells(self, well_pairs: List[Tuple[str, str]]) -> bool:
        """Check if well pairs form a sequential pattern (row or column)."""
        if not well_pairs:
            return False

        # Extract all wells
        wells = [well_pairs[0][0]] + [w[1] for w in well_pairs]

        # Parse well positions
        positions = []
        for well in wells:
            match = re.match(r'([A-P])(\d+)', well)
            if not match:
                return False
            row = ord(match.group(1)) - ord('A')
            col = int(match.group(2))
            positions.append((row, col))

        # Check if sequential in row (same row, increasing column)
        if all(p[0] == positions[0][0] for p in positions):
            cols = [p[1] for p in positions]
            if cols == list(range(cols[0], cols[0] + len(cols))):
                return True

        # Check if sequential in column (same column, increasing row)
        if all(p[1] == positions[0][1] for p in positions):
            rows = [p[0] for p in positions]
            if rows == list(range(rows[0], rows[0] + len(rows))):
                return True

        return False

    def _detect_distribution(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Detect distribution pattern: one source to many destinations.
        """
        if start_idx >= len(self.commands):
            return None, 0

        commands = []
        source_well = None
        source_labware = None
        dest_wells = []
        dest_labware = None
        volume = None
        pipette = None

        i = start_idx
        while i < len(self.commands):
            cmd = self.commands[i]
            cmd_type = cmd.get("command_type", "")

            if cmd_type == "transfer":
                src = cmd.get("source_well")
                dst = cmd.get("dest_well")

                if source_well is None:
                    source_well = src
                    source_labware = cmd.get("source_labware")
                elif src != source_well:
                    break  # Different source, end of distribution

                dest_wells.append(dst)
                dest_labware = dest_labware or cmd.get("dest_labware")
                volume = cmd.get("volume", volume)
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1

            elif cmd_type == "aspirate":
                # Check for aspirate-dispense pattern
                well = cmd.get("well")
                if source_well is None:
                    source_well = well
                    source_labware = cmd.get("labware")
                elif well != source_well:
                    break

                volume = cmd.get("volume", volume)
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1

            elif cmd_type == "dispense":
                dest_wells.append(cmd.get("well"))
                dest_labware = dest_labware or cmd.get("labware")
                commands.append(cmd)
                i += 1

            elif cmd_type in ("pick_up_tip", "drop_tip", "blow_out", "touch_tip"):
                commands.append(cmd)
                i += 1

            else:
                break

        # Need at least 3 destinations for distribution pattern
        if len(dest_wells) >= 3 and source_well:
            return CommandPattern(
                pattern_type=PatternType.DISTRIBUTION,
                commands=commands,
                source_labware=source_labware,
                source_wells=[source_well],
                dest_labware=dest_labware,
                dest_wells=dest_wells,
                volume=volume,
                pipette=pipette
            ), len(commands)

        return None, 0

    def _detect_consolidation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Detect consolidation pattern: many sources to one destination.
        """
        if start_idx >= len(self.commands):
            return None, 0

        commands = []
        source_wells = []
        source_labware = None
        dest_well = None
        dest_labware = None
        volume = None
        pipette = None

        i = start_idx
        while i < len(self.commands):
            cmd = self.commands[i]
            cmd_type = cmd.get("command_type", "")

            if cmd_type == "transfer":
                src = cmd.get("source_well")
                dst = cmd.get("dest_well")

                if dest_well is None:
                    dest_well = dst
                    dest_labware = cmd.get("dest_labware")
                elif dst != dest_well:
                    break

                source_wells.append(src)
                source_labware = source_labware or cmd.get("source_labware")
                volume = cmd.get("volume", volume)
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1

            elif cmd_type in ("pick_up_tip", "drop_tip", "blow_out", "touch_tip", "aspirate", "dispense"):
                commands.append(cmd)
                i += 1

            else:
                break

        if len(source_wells) >= 3 and dest_well:
            return CommandPattern(
                pattern_type=PatternType.CONSOLIDATION,
                commands=commands,
                source_labware=source_labware,
                source_wells=source_wells,
                dest_labware=dest_labware,
                dest_wells=[dest_well],
                volume=volume,
                pipette=pipette
            ), len(commands)

        return None, 0

    def _detect_batch_transfer(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Detect batch transfer: multiple source-dest pairs.
        """
        if start_idx >= len(self.commands):
            return None, 0

        commands = []
        source_wells = []
        dest_wells = []
        source_labware = None
        dest_labware = None
        volume = None
        pipette = None

        i = start_idx
        while i < len(self.commands):
            cmd = self.commands[i]
            cmd_type = cmd.get("command_type", "")

            if cmd_type == "transfer":
                source_wells.append(cmd.get("source_well"))
                dest_wells.append(cmd.get("dest_well"))
                source_labware = source_labware or cmd.get("source_labware")
                dest_labware = dest_labware or cmd.get("dest_labware")
                volume = cmd.get("volume", volume)
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1

            elif cmd_type in ("pick_up_tip", "drop_tip", "blow_out", "touch_tip"):
                commands.append(cmd)
                i += 1

            else:
                break

        if len(source_wells) >= 2:
            return CommandPattern(
                pattern_type=PatternType.TRANSFER_BATCH,
                commands=commands,
                source_labware=source_labware,
                source_wells=source_wells,
                dest_labware=dest_labware,
                dest_wells=dest_wells,
                volume=volume,
                pipette=pipette
            ), len(commands)

        return None, 0

    def _detect_batch_mixing(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """
        Detect batch mixing: mixing multiple wells in sequence.
        """
        if start_idx >= len(self.commands):
            return None, 0

        commands = []
        wells = []
        labware = None
        volume = None
        cycles = None
        pipette = None

        i = start_idx
        while i < len(self.commands):
            cmd = self.commands[i]
            if cmd.get("command_type") == "mix":
                wells.append(cmd.get("well"))
                labware = labware or cmd.get("labware")
                volume = cmd.get("volume", volume)
                cycles = cmd.get("repetitions", cycles)
                pipette = pipette or cmd.get("pipette")
                commands.append(cmd)
                i += 1
            elif cmd.get("command_type") in ("pick_up_tip", "drop_tip"):
                commands.append(cmd)
                i += 1
            else:
                break

        if len(wells) >= 2:
            return CommandPattern(
                pattern_type=PatternType.MIXING_BATCH,
                commands=commands,
                source_labware=labware,
                source_wells=wells,
                mix_cycles=cycles,
                mix_volume=volume,
                pipette=pipette
            ), len(commands)

        return None, 0

    def _detect_temperature_operation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect temperature module operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("set_temperature", "set_block_temperature"):
            return CommandPattern(
                pattern_type=PatternType.TEMPERATURE_CONTROL,
                commands=[cmd],
                module=cmd.get("module"),
                temperature=cmd.get("temperature") or cmd.get("celsius"),
            ), 1

        if cmd_type == "wait_for_temperature":
            return CommandPattern(
                pattern_type=PatternType.TEMPERATURE_CONTROL,
                commands=[cmd],
                module=cmd.get("module"),
                temperature=cmd.get("temperature"),
            ), 1

        if cmd_type == "deactivate_temperature":
            return CommandPattern(
                pattern_type=PatternType.TEMPERATURE_CONTROL,
                commands=[cmd],
                module=cmd.get("module"),
            ), 1

        return None, 0

    def _detect_magnetic_operation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect magnetic module operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("engage_magnets", "engage"):
            return CommandPattern(
                pattern_type=PatternType.MAGNETIC_SEPARATION,
                commands=[cmd],
                module=cmd.get("module"),
                magnet_height=cmd.get("height"),
            ), 1

        if cmd_type in ("disengage_magnets", "disengage"):
            return CommandPattern(
                pattern_type=PatternType.MAGNETIC_SEPARATION,
                commands=[cmd],
                module=cmd.get("module"),
            ), 1

        return None, 0

    def _detect_heater_shaker_operation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect heater-shaker module operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("set_shake_speed", "start_shaking", "shake"):
            return CommandPattern(
                pattern_type=PatternType.HEATER_SHAKER,
                commands=[cmd],
                module=cmd.get("module"),
                shake_speed=cmd.get("rpm") or cmd.get("speed"),
            ), 1

        if cmd_type == "stop_shaking":
            return CommandPattern(
                pattern_type=PatternType.HEATER_SHAKER,
                commands=[cmd],
                module=cmd.get("module"),
            ), 1

        if cmd_type in ("open_latch", "close_latch", "open_labware_latch", "close_labware_latch"):
            return CommandPattern(
                pattern_type=PatternType.HEATER_SHAKER,
                commands=[cmd],
                module=cmd.get("module"),
            ), 1

        return None, 0

    def _detect_thermocycler_operation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect thermocycler operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("set_lid_temperature", "open_lid", "close_lid", "deactivate_lid"):
            return CommandPattern(
                pattern_type=PatternType.THERMOCYCLER,
                commands=[cmd],
                module=cmd.get("module"),
                temperature=cmd.get("temperature"),
            ), 1

        if cmd_type == "run_profile":
            return CommandPattern(
                pattern_type=PatternType.THERMOCYCLER,
                commands=[cmd],
                module=cmd.get("module"),
                cycles=cmd.get("repetitions") or cmd.get("cycles"),
                steps=cmd.get("steps", []),
            ), 1

        return None, 0

    def _detect_incubation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect delay/incubation operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type == "delay":
            seconds = self._safe_float(cmd.get("seconds"), 0)
            minutes = self._safe_float(cmd.get("minutes"), 0)
            total_seconds = seconds + (minutes * 60)

            return CommandPattern(
                pattern_type=PatternType.INCUBATION,
                commands=[cmd],
                duration_seconds=total_seconds,
            ), 1

        return None, 0

    def _detect_single_transfer(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect single transfer operation."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type == "transfer":
            return CommandPattern(
                pattern_type=PatternType.TRANSFER_SINGLE,
                commands=[cmd],
                source_labware=cmd.get("source_labware"),
                source_wells=[cmd.get("source_well")],
                dest_labware=cmd.get("dest_labware"),
                dest_wells=[cmd.get("dest_well")],
                volume=cmd.get("volume"),
                pipette=cmd.get("pipette"),
            ), 1

        # Handle aspirate/dispense pair
        if cmd_type == "aspirate" and start_idx + 1 < len(self.commands):
            next_cmd = self.commands[start_idx + 1]
            if next_cmd.get("command_type") == "dispense":
                return CommandPattern(
                    pattern_type=PatternType.TRANSFER_SINGLE,
                    commands=[cmd, next_cmd],
                    source_labware=cmd.get("labware"),
                    source_wells=[cmd.get("well")],
                    dest_labware=next_cmd.get("labware"),
                    dest_wells=[next_cmd.get("well")],
                    volume=cmd.get("volume"),
                    pipette=cmd.get("pipette"),
                ), 2

        return None, 0

    def _detect_single_mixing(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect single mix operation."""
        cmd = self.commands[start_idx]

        if cmd.get("command_type") == "mix":
            return CommandPattern(
                pattern_type=PatternType.MIXING,
                commands=[cmd],
                source_labware=cmd.get("labware"),
                source_wells=[cmd.get("well")] if cmd.get("well") else [],
                mix_cycles=cmd.get("repetitions"),
                mix_volume=cmd.get("volume"),
                pipette=cmd.get("pipette"),
            ), 1

        return None, 0

    def _detect_tip_operation(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect tip operations."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("pick_up_tip", "drop_tip", "return_tip"):
            return CommandPattern(
                pattern_type=PatternType.TIP_OPERATION,
                commands=[cmd],
                pipette=cmd.get("pipette"),
            ), 1

        return None, 0

    def _detect_pause_comment(self, start_idx: int) -> Tuple[Optional[CommandPattern], int]:
        """Detect pause or comment."""
        cmd = self.commands[start_idx]
        cmd_type = cmd.get("command_type", "")

        if cmd_type in ("pause", "comment"):
            return CommandPattern(
                pattern_type=PatternType.PAUSE if cmd_type == "pause" else PatternType.COMMENT,
                commands=[cmd],
            ), 1

        return None, 0

    def _merge_adjacent_patterns(self, patterns: List[CommandPattern]) -> List[CommandPattern]:
        """Merge adjacent patterns of the same type where appropriate."""
        if not patterns:
            return patterns

        merged = []
        current = patterns[0]

        for next_pattern in patterns[1:]:
            # Try to merge similar patterns
            if self._can_merge(current, next_pattern):
                current = self._merge_patterns(current, next_pattern)
            else:
                merged.append(current)
                current = next_pattern

        merged.append(current)
        return merged

    def _can_merge(self, p1: CommandPattern, p2: CommandPattern) -> bool:
        """Check if two patterns can be merged."""
        # Merge consecutive tip operations
        if p1.pattern_type == PatternType.TIP_OPERATION and p2.pattern_type == PatternType.TIP_OPERATION:
            return True

        # Merge consecutive similar transfers with same labware and volume
        if (p1.pattern_type == PatternType.TRANSFER_SINGLE and
            p2.pattern_type == PatternType.TRANSFER_SINGLE and
            p1.source_labware == p2.source_labware and
            p1.dest_labware == p2.dest_labware and
            p1.volume == p2.volume):
            return True

        return False

    def _merge_patterns(self, p1: CommandPattern, p2: CommandPattern) -> CommandPattern:
        """Merge two patterns into one."""
        if p1.pattern_type == PatternType.TIP_OPERATION:
            return CommandPattern(
                pattern_type=PatternType.TIP_OPERATION,
                commands=p1.commands + p2.commands,
                pipette=p1.pipette or p2.pipette,
            )

        if p1.pattern_type == PatternType.TRANSFER_SINGLE:
            return CommandPattern(
                pattern_type=PatternType.TRANSFER_BATCH,
                commands=p1.commands + p2.commands,
                source_labware=p1.source_labware,
                source_wells=p1.source_wells + p2.source_wells,
                dest_labware=p1.dest_labware,
                dest_wells=p1.dest_wells + p2.dest_wells,
                volume=p1.volume,
                pipette=p1.pipette,
            )

        return p1  # Fallback: don't merge
