from pydantic import BaseModel, Field, model_validator, field_validator, ValidationInfo
from typing import List, Optional, Literal, Union, Annotated, Dict, Any


# ============================================================================
# LABWARE & PIPETTE DEFINITIONS
# ============================================================================

class Labware(BaseModel):
    slot: str = Field(..., description="The deck slot (1-11, or D1-D3 for Flex)")
    load_name: str = Field(..., description="The Opentrons API name for the labware (e.g., 'corning_96_wellplate_360ul_flat')")
    label: Optional[str] = Field(None, description="A human-readable name to reference this labware in commands")


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
        # Complex commands
        Transfer,
        Distribute,
        Consolidate,
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
    commands: List[Command]

    @field_validator('labware')
    @classmethod
    def validate_labware_against_config(cls, v: List[Labware], info: ValidationInfo) -> List[Labware]:
        """Validate that protocol labware is a subset of lab config."""
        config = info.context.get('config') if info.context else None
        if not config or 'labware' not in config:
            return v

        config_set = {
            (lw['load_name'], lw['slot'], lw.get('label', ''))
            for lw in config['labware'].values()
        }
        protocol_set = {
            (lw.load_name, lw.slot, lw.label or '')
            for lw in v
        }

        if not protocol_set <= config_set:
            invalid = protocol_set - config_set
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
        mount_to_capacity: Dict[str, tuple] = {}
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

            # Check labware references exist and resolve them
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
                min_vol, max_vol = mount_to_capacity[cmd.pipette]

                # Check main volume field
                if hasattr(cmd, 'volume') and cmd.volume is not None:
                    if cmd.volume < min_vol or cmd.volume > max_vol:
                        raise ValueError(
                            f"Command {i}: volume {cmd.volume}µL outside pipette range ({min_vol}-{max_vol}µL)"
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

        return self
