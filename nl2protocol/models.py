import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, model_validator, field_validator, ValidationInfo
from typing import List, Optional, Literal, Union, Annotated, Dict, Any

"""
crucial file for STEP 2

main artifact: Protocol Schema, and its validation capabilities
- at a high level, using the defined pydantic class to verify the outputted json to see if:
    - each labware item defined exaclty as in config and not otherwise (1)
    - each pipette item defined exactly as in config and not otherwise (2)
    - for cmds
        - (1), (2) for all its labware and pipette field
        - voulume related cmds are within its pipette bounds

can we also
- look at starting volumes and capacities so we:
    - dont overdispense into well #NOTE
    - overexpect from reservoirs #NOTE

at a high level, protocol schema model validation does this;
takes: json input
returns: protocol schema object, which if it exist succesful is heavily validated by (atomic) liquid, and actual config/setup
    constraints
"""


# ============================================================================
# INSTRUCTION CONSTRAINTS (deterministic, no LLM)
# ============================================================================

@dataclass
class InstructionConstraints:
    """Deterministic regex extraction of hard facts from user instruction.
    No LLM involved -- pure pattern matching.

    Volumes that are hedged (e.g. 'about 100uL', '~25uL') are excluded
    from hard constraints since the user indicated flexibility.
    """
    volumes: List[float] = field(default_factory=list)

    # Matches hedged volumes: "about 100uL", "approximately 50uL", "~25uL", "around 10uL"
    _HEDGE_PATTERN = re.compile(
        r'(?:about|approximately|approx\.?|around|~|roughly)\s+(\d+(?:\.\d+)?)\s*(?:u[lL]|µ[lL])',
        re.IGNORECASE
    )
    # Matches all volumes: 10.5uL, 10.5µL, 10.5 uL, etc.
    _VOLUME_PATTERN = re.compile(
        r'(\d+(?:\.\d+)?)\s*(?:u[lL]|µ[lL])',
    )

    @classmethod
    def from_instruction(cls, instruction: str) -> 'InstructionConstraints':
        # First, find hedged volumes to exclude them
        hedged_volumes = set()
        for m in cls._HEDGE_PATTERN.finditer(instruction):
            hedged_volumes.add(float(m.group(1)))

        # Then find all volumes, excluding hedged ones
        volumes = []
        for m in cls._VOLUME_PATTERN.finditer(instruction):
            vol = float(m.group(1))
            if vol not in hedged_volumes:
                volumes.append(vol)

        return cls(volumes=volumes)


# ============================================================================
# LABWARE & PIPETTE DEFINITIONS
# ============================================================================

class Labware(BaseModel):
    slot: str = Field(..., description="The deck slot (1-11, or D1-D3 for Flex). If on_module is set, this should match the module's slot.")
    load_name: str = Field(..., description="The Opentrons API name for the labware (e.g., 'corning_96_wellplate_360ul_flat')")
    label: Optional[str] = Field(None, description="A human-readable name to reference this labware in commands")
    on_module: Optional[str] = Field(None, description="Module label or slot to load this labware onto (e.g., 'mag_mod' or '6'). If set, labware is loaded onto the module instead of directly on the deck.")


class Pipette(BaseModel):
    mount: Literal["left", "right"]
    model: str = Field(..., description="The pipette model (e.g., 'p300_single_gen2', 'p1000_single_flex')")
    tipracks: List[str] = Field(default_factory=list, description="List of labware labels or slots containing tipracks for this pipette")


# ============================================================================
# BUILDING BLOCK (ATOMIC) COMMANDS
# ============================================================================

class Aspirate(BaseModel):
    """Pull liquid into the pipette tip."""
    command_type: Literal["aspirate"] = "aspirate"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: str = Field(..., description="Label or slot of the labware")
    well: str = Field(..., description="Well position (e.g., 'A1', 'B3')")
    volume: float = Field(..., gt=0, description="Volume in microliters")


class Dispense(BaseModel):
    """Push liquid out of the pipette tip."""
    command_type: Literal["dispense"] = "dispense"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: str = Field(..., description="Label or slot of the labware")
    well: str = Field(..., description="Well position (e.g., 'A1', 'B3')")
    volume: float = Field(..., gt=0, description="Volume in microliters")


class BlowOut(BaseModel):
    """Expel any remaining liquid/air from the tip."""
    command_type: Literal["blow_out"] = "blow_out"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: Optional[str] = Field(None, description="Label or slot (if None, blows out at current position)")
    well: Optional[str] = Field(None, description="Well position (if None, blows out at current position)")


class TouchTip(BaseModel):
    """Touch the tip to the sides of a well to knock off droplets."""
    command_type: Literal["touch_tip"] = "touch_tip"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: str = Field(..., description="Label or slot of the labware")
    well: str = Field(..., description="Well position")


class Mix(BaseModel):
    """Aspirate and dispense repeatedly to mix liquid in a well."""
    command_type: Literal["mix"] = "mix"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: str = Field(..., description="Label or slot of the labware")
    well: str = Field(..., description="Well position")
    volume: float = Field(..., gt=0, description="Volume to aspirate/dispense each cycle")
    repetitions: int = Field(..., ge=1, description="Number of mix cycles")


class AirGap(BaseModel):
    """Add an air gap to the tip (prevents dripping during moves)."""
    command_type: Literal["air_gap"] = "air_gap"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    volume: float = Field(..., gt=0, description="Air gap volume in microliters")


class PickUpTip(BaseModel):
    """Pick up a pipette tip."""
    command_type: Literal["pick_up_tip"] = "pick_up_tip"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: Optional[str] = Field(None, description="Tiprack label or slot (if None, uses next available tip)")
    well: Optional[str] = Field(None, description="Specific tip position (if None, uses next available)")


class DropTip(BaseModel):
    """Drop the current pipette tip."""
    command_type: Literal["drop_tip"] = "drop_tip"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    labware: Optional[str] = Field(None, description="Where to drop (if None, uses trash)")
    well: Optional[str] = Field(None, description="Specific position to drop")


class ReturnTip(BaseModel):
    """Return the tip to its original location (for tip reuse)."""
    command_type: Literal["return_tip"] = "return_tip"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")


# ============================================================================
# FLOW CONTROL COMMANDS
# ============================================================================

class Pause(BaseModel):
    """Pause the protocol for manual intervention."""
    command_type: Literal["pause"] = "pause"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context (for command tracking)")
    message: str = Field(..., description="Message to display to user during pause")


class Delay(BaseModel):
    """Insert a delay/wait in the protocol."""
    command_type: Literal["delay"] = "delay"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context (for command tracking)")
    seconds: Optional[float] = Field(None, ge=0, description="Delay in seconds")
    minutes: Optional[float] = Field(None, ge=0, description="Delay in minutes")

    @model_validator(mode='after')
    def validate_delay_time(self) -> 'Delay':
        if self.seconds is None and self.minutes is None:
            raise ValueError("Either 'seconds' or 'minutes' must be specified")
        return self


class Comment(BaseModel):
    """Add a comment to the run log."""
    command_type: Literal["comment"] = "comment"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context (for command tracking)")
    message: str = Field(..., description="Comment text to log")


# ============================================================================
# COMPLEX (COMPOUND) COMMANDS
# ============================================================================

class Transfer(BaseModel):
    """Move liquid from source to destination. Handles tip pickup/drop automatically."""
    command_type: Literal["transfer"] = "transfer"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    source_labware: str = Field(..., description="Label or slot of source labware")
    source_well: str = Field(..., description="Source well position")
    dest_labware: str = Field(..., description="Label or slot of destination labware")
    dest_well: str = Field(..., description="Destination well position")
    volume: float = Field(..., gt=0, description="Volume in microliters")
    new_tip: Literal["always", "once", "never"] = Field("always", description="Tip changing strategy")
    mix_before: Optional[tuple[int, float]] = Field(None, description="(repetitions, volume) to mix before aspirating")
    mix_after: Optional[tuple[int, float]] = Field(None, description="(repetitions, volume) to mix after dispensing")


class Distribute(BaseModel):
    """Distribute liquid from one source to multiple destinations (one aspiration, multiple dispenses)."""
    command_type: Literal["distribute"] = "distribute"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    source_labware: str = Field(..., description="Label or slot of source labware")
    source_well: str = Field(..., description="Source well position")
    dest_labware: str = Field(..., description="Label or slot of destination labware")
    dest_wells: List[str] = Field(..., description="List of destination well positions")
    volume: float = Field(..., gt=0, description="Volume to dispense into each destination")
    new_tip: Literal["always", "once", "never"] = Field("once", description="Tip changing strategy")


class Consolidate(BaseModel):
    """Consolidate liquid from multiple sources to one destination (multiple aspirations, one dispense)."""
    command_type: Literal["consolidate"] = "consolidate"
    pipette: Literal["left", "right"] = Field(..., description="Which pipette to use")
    source_labware: str = Field(..., description="Label or slot of source labware")
    source_wells: List[str] = Field(..., description="List of source well positions")
    dest_labware: str = Field(..., description="Label or slot of destination labware")
    dest_well: str = Field(..., description="Destination well position")
    volume: float = Field(..., gt=0, description="Volume to aspirate from each source")
    new_tip: Literal["always", "once", "never"] = Field("once", description="Tip changing strategy")


# ============================================================================
# HARDWARE MODULES
# ============================================================================

class Module(BaseModel):
    """Hardware module attached to the deck."""
    module_type: Literal["temperature", "magnetic", "heater_shaker", "thermocycler"] = Field(
        ..., description="Type of module"
    )
    slot: str = Field(..., description="Deck slot where module is placed")
    label: Optional[str] = Field(None, description="Label to reference this module")


# ============================================================================
# MODULE COMMANDS
# ============================================================================

class SetTemperature(BaseModel):
    """Set temperature on a temperature module or heater-shaker."""
    command_type: Literal["set_temperature"] = "set_temperature"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    celsius: float = Field(..., description="Target temperature in Celsius")


class WaitForTemperature(BaseModel):
    """Wait for module to reach target temperature."""
    command_type: Literal["wait_for_temperature"] = "wait_for_temperature"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class DeactivateModule(BaseModel):
    """Deactivate/turn off a module."""
    command_type: Literal["deactivate"] = "deactivate"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class EngageMagnets(BaseModel):
    """Engage magnets on a magnetic module."""
    command_type: Literal["engage_magnets"] = "engage_magnets"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    height: Optional[float] = Field(None, description="Height in mm from labware bottom")


class DisengageMagnets(BaseModel):
    """Disengage/lower magnets on a magnetic module."""
    command_type: Literal["disengage_magnets"] = "disengage_magnets"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class SetShakeSpeed(BaseModel):
    """Set shake speed on a heater-shaker module."""
    command_type: Literal["set_shake_speed"] = "set_shake_speed"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    rpm: int = Field(..., ge=0, description="Shake speed in RPM (0 to stop)")


class OpenLatch(BaseModel):
    """Open the latch on a heater-shaker module."""
    command_type: Literal["open_latch"] = "open_latch"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class CloseLatch(BaseModel):
    """Close the latch on a heater-shaker module."""
    command_type: Literal["close_latch"] = "close_latch"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class OpenLid(BaseModel):
    """Open the lid on a thermocycler module."""
    command_type: Literal["open_lid"] = "open_lid"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class CloseLid(BaseModel):
    """Close the lid on a thermocycler module."""
    command_type: Literal["close_lid"] = "close_lid"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")


class SetBlockTemperature(BaseModel):
    """Set thermocycler block temperature."""
    command_type: Literal["set_block_temperature"] = "set_block_temperature"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    celsius: float = Field(..., description="Target temperature in Celsius")
    hold_time_seconds: Optional[float] = Field(None, description="Hold time in seconds")
    hold_time_minutes: Optional[float] = Field(None, description="Hold time in minutes")


class SetLidTemperature(BaseModel):
    """Set thermocycler lid temperature."""
    command_type: Literal["set_lid_temperature"] = "set_lid_temperature"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    celsius: float = Field(..., description="Target temperature in Celsius")


class RunProfile(BaseModel):
    """Run a thermocycler temperature profile (PCR cycles)."""
    command_type: Literal["run_profile"] = "run_profile"
    pipette: Literal["left", "right"] = Field(..., description="Pipette context")
    module: str = Field(..., description="Module label or slot")
    steps: List[Dict[str, Any]] = Field(..., description="List of {temperature, hold_time_seconds} steps")
    repetitions: int = Field(1, ge=1, description="Number of times to repeat the profile")


# ============================================================================
# DISCRIMINATED UNION OF ALL COMMANDS
# ============================================================================

Command = Annotated[
    Union[
        # Atomic commands
        Aspirate,
        Dispense,
        BlowOut,
        TouchTip,
        Mix,
        AirGap,
        PickUpTip,
        DropTip,
        ReturnTip,
        # Flow control commands
        Pause,
        Delay,
        Comment,
        # Complex commands
        Transfer,
        Distribute,
        Consolidate,
        # Module commands
        SetTemperature,
        WaitForTemperature,
        DeactivateModule,
        EngageMagnets,
        DisengageMagnets,
        SetShakeSpeed,
        OpenLatch,
        CloseLatch,
        OpenLid,
        CloseLid,
        SetBlockTemperature,
        SetLidTemperature,
        RunProfile,
    ],
    Field(discriminator="command_type")
]


# ============================================================================
# PROTOCOL SCHEMA
# ============================================================================

class ProtocolSchema(BaseModel):
    """The complete protocol definition that can be converted to an Opentrons script."""
    protocol_name: str = "AI Generated Protocol"
    author: str = "Biolab AI"
    labware: List[Labware]
    pipettes: List[Pipette]
    modules: Optional[List[Module]] = Field(default_factory=list, description="Hardware modules")
    commands: List[Command]

    @field_validator('labware')
    @classmethod
    def validate_output_against_input_config(cls, v: List[Labware], info: ValidationInfo) -> List[Labware]:
        """Validate that protocol labware is a subset of lab config.

        Labware can be loaded either:
        1. Directly on a deck slot (must be in config labware)
        2. On a module (on_module field set, must match config labware with on_module)
        """
        config = info.context.get('config') if info.context else None
        if not config or 'labware' not in config:
            return v

        # Build config labware set including on_module info
        # Tuple: (load_name, slot, label, on_module)
        config_set = {
            (lw['load_name'], lw['slot'], lw.get('label', ''), lw.get('on_module', ''))
            for lw in config['labware'].values()
        }

        # Also build a mapping of config labware by label for reference
        config_by_label = {}
        for label, lw in config['labware'].items():
            config_by_label[label] = lw

        # Build set of valid module references (both slot and label)
        module_refs = set()
        if 'modules' in config:
            for label, mod in config['modules'].items():
                module_refs.add(label)  # Module label
                module_refs.add(mod['slot'])  # Module slot

        invalid = set()
        for lw in v:
            lw_tuple = (lw.load_name, lw.slot, lw.label or '', lw.on_module or '')

            if lw_tuple not in config_set:
                # Check if it's a labware-on-module that matches config by label
                if lw.label and lw.label in config_by_label:
                    config_lw = config_by_label[lw.label]
                    # Allow if the config labware is on the same module
                    config_tuple = (config_lw['load_name'], config_lw['slot'],
                                    config_lw.get('label', ''), config_lw.get('on_module', ''))
                    if lw_tuple != config_tuple:
                        invalid.add(lw_tuple)
                else:
                    invalid.add(lw_tuple)

        if invalid:
            raise ValueError(f"Labware not in config: {invalid}")
        return v

    @field_validator('pipettes')
    @classmethod
    def validate_pipettes_against_config(cls, v: List[Pipette], info: ValidationInfo) -> List[Pipette]:
        """Validate that protocol pipettes is a subset of lab config."""
        config = info.context.get('config') if info.context else None
        if not config or 'pipettes' not in config:
            return v

        config_set = {
            (mount, pip['model'], frozenset(pip.get('tipracks', [])))
            for mount, pip in config['pipettes'].items()
        }
        protocol_set = {
            (pip.mount, pip.model, frozenset(pip.tipracks))
            for pip in v
        }

        if not protocol_set <= config_set:
            invalid = protocol_set - config_set
            raise ValueError(f"Pipettes not in config: {invalid}")
        return v

    @field_validator('modules')
    @classmethod
    def validate_modules_against_config(cls, v: Optional[List[Module]], info: ValidationInfo) -> Optional[List[Module]]:
        """Validate that protocol modules is a subset of lab config."""
        if not v:
            return v

        config = info.context.get('config') if info.context else None
        if not config or 'modules' not in config:
            return v

        config_set = {
            (mod['module_type'], mod['slot'])
            for mod in config['modules'].values()
        }
        protocol_set = {
            (mod.module_type, mod.slot)
            for mod in v
        }

        if not protocol_set <= config_set:
            invalid = protocol_set - config_set
            raise ValueError(f"Modules not in config: {invalid}")
        return v

    @model_validator(mode='after')
    def validate_references(self, info: ValidationInfo) -> 'ProtocolSchema':
        # =================================================================
        # Build labware lookup: reference (slot or label) → Labware object
        # =================================================================
        labware_lookup: Dict[str, Labware] = {}
        for lw in self.labware:
            labware_lookup[lw.slot] = lw
            if lw.label:
                labware_lookup[lw.label] = lw

        # =================================================================
        # Build pipette lookup: mount → Pipette object
        # =================================================================
        pipette_lookup: Dict[str, Pipette] = {p.mount: p for p in self.pipettes}

        # =================================================================
        # Build module lookup: reference (slot or label) → Module object
        # =================================================================
        module_lookup: Dict[str, Module] = {}
        if self.modules:
            for mod in self.modules:
                module_lookup[mod.slot] = mod
                if mod.label:
                    module_lookup[mod.label] = mod

        # Module parameter ranges (min, max)
        MODULE_TEMP_RANGES = {
            "temperature": (4, 95),      # Temperature module: 4-95°C
            "heater_shaker": (37, 95),   # Heater-shaker: 37-95°C
            "thermocycler": (4, 99),     # Thermocycler block: 4-99°C
        }
        THERMOCYCLER_LID_RANGE = (37, 110)  # Lid: 37-110°C
        HEATER_SHAKER_RPM_RANGE = (200, 3000)  # 200-3000 RPM

        # Pipette capacity lookup (min, max) in µL
        # Order matters: check longer names first to avoid "p10" matching "p1000"
        pipette_capacities = [
            ('p1000', (100, 1000)),
            ('p300', (20, 300)),
            ('p50', (5, 50)),
            ('p20', (1, 20)),
            ('p10', (1, 10)),
        ]

        # Map mount → capacity based on pipette model
        mount_to_capacity: Dict[str, tuple] = {} # a dictionary of pipette address, capacity
        for mount, pip in pipette_lookup.items():
            for model_key, capacity in pipette_capacities:
                if model_key in pip.model.lower():
                    mount_to_capacity[mount] = capacity
                    break

        # =================================================================
        # Validate each command
        # =================================================================
        for i, cmd in enumerate(self.commands):
            # Check pipette exists
            if cmd.pipette not in pipette_lookup:
                raise ValueError(
                    f"Command {i}: pipette '{cmd.pipette}' not defined. "
                    f"Available: {set(pipette_lookup.keys())}"
                )

            # for cmd, does labware exist for all its labware field?... if not raise error
            labware_fields = ['labware', 'source_labware', 'dest_labware']
            for field in labware_fields:
                if hasattr(cmd, field):
                    ref = getattr(cmd, field)
                    if ref is not None and ref not in labware_lookup:
                        raise ValueError(
                            f"Command {i}: {field} '{ref}' not defined. "
                            f"Available: {set(labware_lookup.keys())}"
                        )

            # Check volume is within pipette capacity
            if cmd.pipette in mount_to_capacity:
                min_vol, max_vol = mount_to_capacity[cmd.pipette] # since capacity is a coordinate

                # Check main volume field
                if hasattr(cmd, 'volume') and cmd.volume is not None:
                    if cmd.volume < min_vol or cmd.volume > max_vol:
                        # Find which pipettes CAN handle this volume
                        suitable = []
                        for mount, cap in mount_to_capacity.items():
                            if cap[0] <= cmd.volume <= cap[1]:
                                suitable.append(f"{mount} ({pipette_lookup[mount].model}, {cap[0]}-{cap[1]}µL)")

                        suggestion = ""
                        if suitable:
                            suggestion = (
                                f" Available pipettes that CAN handle {cmd.volume}µL: "
                                f"{', '.join(suitable)}. "
                                f"DO NOT change the volume — change the pipette assignment to one of these instead."
                            )
                        else:
                            suggestion = (
                                f" No available pipette can handle {cmd.volume}µL. "
                                f"Available: {', '.join(f'{m} ({p.model})' for m, p in pipette_lookup.items())}."
                            )

                        raise ValueError(
                            f"Command {i}: volume {cmd.volume}µL outside pipette '{cmd.pipette}' range "
                            f"({min_vol}-{max_vol}µL).{suggestion}"
                        )

                # Check mix_before volume [reps, volume]
                if hasattr(cmd, 'mix_before') and cmd.mix_before is not None:
                    mix_vol = cmd.mix_before[1]
                    if mix_vol < min_vol or mix_vol > max_vol:
                        raise ValueError(
                            f"Command {i}: mix_before volume {mix_vol}µL outside pipette range ({min_vol}-{max_vol}µL)"
                        )

                # Check mix_after volume [reps, volume]
                if hasattr(cmd, 'mix_after') and cmd.mix_after is not None:
                    mix_vol = cmd.mix_after[1]
                    if mix_vol < min_vol or mix_vol > max_vol:
                        raise ValueError(
                            f"Command {i}: mix_after volume {mix_vol}µL outside pipette range ({min_vol}-{max_vol}µL)"
                        )

            # =============================================================
            # Validate module commands
            # =============================================================
            if hasattr(cmd, 'module'):
                module_ref = cmd.module
                if module_ref not in module_lookup:
                    raise ValueError(
                        f"Command {i}: module '{module_ref}' not defined. "
                        f"Available: {set(module_lookup.keys()) if module_lookup else 'none'}"
                    )

                mod = module_lookup[module_ref]

                # Validate temperature ranges
                if hasattr(cmd, 'celsius'):
                    temp = cmd.celsius
                    if cmd.command_type in ('set_temperature', 'set_block_temperature'):
                        temp_range = MODULE_TEMP_RANGES.get(mod.module_type)
                        if temp_range:
                            min_temp, max_temp = temp_range
                            if temp < min_temp or temp > max_temp:
                                raise ValueError(
                                    f"Command {i}: temperature {temp}°C outside {mod.module_type} range ({min_temp}-{max_temp}°C)"
                                )
                    elif cmd.command_type == 'set_lid_temperature':
                        min_temp, max_temp = THERMOCYCLER_LID_RANGE
                        if temp < min_temp or temp > max_temp:
                            raise ValueError(
                                f"Command {i}: lid temperature {temp}°C outside range ({min_temp}-{max_temp}°C)"
                            )

                # Validate heater-shaker RPM
                if hasattr(cmd, 'rpm') and cmd.rpm is not None:
                    rpm = cmd.rpm
                    min_rpm, max_rpm = HEATER_SHAKER_RPM_RANGE
                    if rpm != 0 and (rpm < min_rpm or rpm > max_rpm):
                        raise ValueError(
                            f"Command {i}: RPM {rpm} outside heater-shaker range ({min_rpm}-{max_rpm} RPM, or 0 to stop)"
                        )

        return self
