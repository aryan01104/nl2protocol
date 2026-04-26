"""
models — Data models for nl2protocol.

Two layers:
  spec.py  — "science language" (ProtocolSpec, ExtractedStep, provenance types)
  schema.py — "hardware language" (ProtocolSchema, Labware, Pipette, Command types)

The LLM produces a ProtocolSpec. spec_to_schema() converts it to a ProtocolSchema.
ProtocolSchema gets converted to a Python script.
"""

from nl2protocol.models.spec import (
    WellName,
    Provenance,
    CompositionProvenance,
    ProvenancedVolume,
    ProvenancedDuration,
    ProvenancedTemperature,
    ProvenancedString,
    LocationRef,
    PostAction,
    ActionType,
    ExtractedStep,
    WellContents,
    LabwarePrefill,
    ProtocolSpec,
    CompleteProtocolSpec,
)

from nl2protocol.models.schema import (
    Labware,
    Pipette,
    Aspirate,
    Dispense,
    BlowOut,
    TouchTip,
    Mix,
    AirGap,
    PickUpTip,
    DropTip,
    ReturnTip,
    Pause,
    Delay,
    Comment,
    Transfer,
    Distribute,
    Consolidate,
    Module,
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
    Command,
    ProtocolSchema,
)
