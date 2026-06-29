"""Render a Bradford-assay report through reporting2 + report4 for a live look.

Scratch only (untracked). Builds the spec from the Claude-design mockup
(5 transfers, BSA stock from tube-rack D6 -> assay-plate A12..E12), emits the
pipeline events the HTMLReporter consumes, and writes the HTML. Uses
nl2protocol.reporting2 (which now loads report4.html.jinja); does NOT touch
report.html.jinja or reporting.py.
"""
from nl2protocol.reporting2 import HTMLReporter
from nl2protocol.reporting import StageEvent
from nl2protocol.models.spec import (
    CompositionProvenance, ExtractedStep, InstructionProvenance,
    LocationRef, ProtocolSpec, ProvenancedString, ProvenancedVolume,
)

INSTRUCTION = """Perform a Bradford protein assay with 8 unknown samples and a BSA standard curve.

SETUP
Tube rack D6 contains BSA stock solution (2 mg/mL).
96-well assay plate in slot 3 - reagent reservoir in slot 5.

PROTOCOL - PREPARE BSA STANDARD CURVE IN COLUMN 12
A12: Add 10uL BSA stock
B12: Add 5uL BSA stock + 5uL water
C12: Add 2.5uL BSA stock + 7.5uL water
D12: Add 1.25uL BSA stock + 8.75uL water
E12: Add 1uL BSA stock + 9uL water"""

# (destination well, volume number, volume cited phrase)
ROWS = [
    ("A12", 10.0, "10uL"),
    ("B12", 5.0, "5uL"),
    ("C12", 2.5, "2.5uL"),
    ("D12", 1.25, "1.25uL"),
    ("E12", 1.0, "1uL"),
]


def _prov(cited):
    return InstructionProvenance(source="instruction", cited_text=[cited], confidence=1.0)


def _build_spec():
    steps = []
    for i, (dest_well, vol, vol_cited) in enumerate(ROWS):
        vp = _prov(vol_cited)
        sp = _prov("BSA stock")
        src_p = _prov("Tube rack")
        src_wp = _prov("D6")
        dst_p = _prov("assay plate")
        dst_wp = _prov(dest_well)
        steps.append(ExtractedStep(
            order=i + 1,
            action="transfer",
            volume=ProvenancedVolume(value=vol, unit="uL", exact=True, provenance=vp),
            substance=ProvenancedString(value="BSA stock", provenance=sp),
            source=LocationRef(
                description="tube rack", well="D6", resolved_label="sample_rack",
                description_provenance=src_p, wells_provenance=src_wp,
            ),
            destination=LocationRef(
                description="96 well plate", well=dest_well, resolved_label="assay_plate",
                description_provenance=dst_p, wells_provenance=dst_wp,
            ),
            composition_provenance=CompositionProvenance(
                step_cited_text=f"{dest_well}: Add {vol_cited} BSA stock",
                parameters_cited_texts=[vol_cited, "BSA stock", dest_well],
                parameters_reasoning=(
                    "the single instruction line names the volume, substance, and "
                    "destination well; the source well grounds to the setup map."
                ),
                grounding=["instruction"],
                confidence=1.0,
            ),
        ))
    return ProtocolSpec(summary="Bradford BSA standard curve", steps=steps)


def _build_script():
    prelude = [
        "from opentrons import protocol_api",
        "",
        "def run(protocol):",
        "    lw_2 = protocol.load_labware('opentrons_24_tuberack', '2')",
        "    lw_3 = protocol.load_labware('corning_96_wellplate', '3')",
        "    pip_left = protocol.load_instrument('p20_single_gen2', 'left')",
        "",
    ]
    lines = list(prelude)
    step_line_map = {}
    for i, (dest_well, vol, _c) in enumerate(ROWS):
        step_line_map[i] = [len(lines)]
        lines.append(f"    pip_left.transfer({vol}, lw_2['D6'], lw_3['{dest_well}'])")
    return "\n".join(lines), step_line_map


def main():
    out = "output/report4_live_demo.html"
    spec = _build_spec()
    script, step_line_map = _build_script()
    r = HTMLReporter(out)
    r.emit(StageEvent(kind="raw_instruction", data={"instruction": INSTRUCTION}))
    r.emit(StageEvent(kind="extracted_spec", data={"spec": spec}))
    r.emit(StageEvent(kind="completed_spec", data={"spec": spec}))
    r.emit(StageEvent(kind="generated_script",
                      data={"script": script, "step_line_map": step_line_map}))
    r.finalize()
    print("WROTE", out)


if __name__ == "__main__":
    main()
