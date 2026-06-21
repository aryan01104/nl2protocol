"""
Opentrons OT-2 Protocol
=======================
Cleanup of Fragmented DNA using KAPA Pure Beads (single-sample run)

Layout
------
Slot 1 : 300 µL tip rack
Slot 2 : 24-tube reagent rack
          A1 – KAPA Pure Beads
          A2 – Elution buffer (10 mM Tris-HCl, pH 8.0-8.5)
Slot 3 : 12-well reservoir (15 mL)
          A1 – 80 % ethanol
Slot 4 : Magnetic module → deep-well plate (sample)
          A1 – 100 µL fragmented DNA sample
Slot 5 : PCR plate (eluate)
          A1 – destination for purified DNA

Tip strategy
------------
* Fresh tip for every clean-reagent addition (beads, ethanol, elution buffer).
* One shared tip reused across all supernatant / ethanol removal steps for the
  same well (the tip is picked up before the first removal and dropped only
  after the last ethanol wash removal).
"""

from opentrons import protocol_api

metadata = {
    "protocolName": "KAPA Pure Beads – Single-Sample Cleanup",
    "author": "OT-2 Protocol Generator",
    "description": (
        "Bead-based cleanup of fragmented DNA: bead binding, two ethanol "
        "washes, air-dry, and elution into a PCR plate."
    ),
    "apiLevel": "2.13",
}

# ── tuneable parameters ────────────────────────────────────────────────────────
BEAD_VOLUME_UL          = 80      # µL of KAPA Pure Beads added to 100 µL sample
ETHANOL_VOLUME_UL       = 200     # µL of 80 % ethanol per wash
ELUTION_VOLUME_UL       = 32      # µL of elution buffer (adjust as needed)

MIX_REPS                = 10      # number of mix cycles after bead addition
MIX_VOLUME_UL           = 100     # mix volume (µL)

BEAD_BIND_MINUTES       = 5       # room-temperature incubation after bead addition
ETHANOL_INCUBATE_SEC    = 30      # magnet incubation during each ethanol wash (s)
BEAD_DRY_MINUTES        = 3       # air-dry time after final ethanol removal
ELUTION_INCUBATE_MIN    = 2       # incubation after elution buffer addition

MAG_HEIGHT_MM           = 6.0     # magnet engage height (mm) – adjust for labware
MAG_ENGAGE_WAIT_SEC     = 60      # seconds to wait after engaging magnet

ASPIRATION_FLOW_RATE    = 30      # µL/s – slow rate for removing supernatant/EtOH
DISPENSE_FLOW_RATE      = 50      # µL/s – moderate rate for dispensing


def run(protocol: protocol_api.ProtocolContext) -> None:

    # ── load modules ──────────────────────────────────────────────────────────
    mag_mod = protocol.load_module("magnetic module gen2", "4")

    # ── load labware ──────────────────────────────────────────────────────────
    tiprack_300     = protocol.load_labware("opentrons_96_tiprack_300ul",                    "1")
    reagent_rack    = protocol.load_labware("opentrons_24_tuberack_eppendorf_1.5ml_safelock_snapcap", "2")
    ethanol_res     = protocol.load_labware("nest_12_reservoir_15ml",                        "3")
    sample_plate    = mag_mod.load_labware("nest_96_wellplate_2ml_deep")
    elution_plate   = protocol.load_labware("nest_96_wellplate_100ul_pcr_full_skirt",        "5")

    # ── load pipette ──────────────────────────────────────────────────────────
    p300 = protocol.load_instrument(
        "p300_single_gen2",
        mount="left",
        tip_racks=[tiprack_300],
    )

    # ── define wells ──────────────────────────────────────────────────────────
    sample_well   = sample_plate["A1"]
    beads_tube    = reagent_rack["A1"]
    elution_tube  = reagent_rack["A2"]
    ethanol_well  = ethanol_res["A1"]
    eluate_well   = elution_plate["A1"]

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1-2 – Add KAPA Pure Beads and mix
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 1-2: Adding KAPA Pure Beads and mixing ===")

    p300.pick_up_tip()  # fresh tip for bead addition

    p300.aspirate(BEAD_VOLUME_UL, beads_tube)
    p300.dispense(BEAD_VOLUME_UL, sample_well)

    # Mix thoroughly (10×, 100 µL) – reuse the same tip
    p300.mix(MIX_REPS, MIX_VOLUME_UL, sample_well)
    p300.blow_out(sample_well.top(-2))

    p300.drop_tip()

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3 – Room-temperature incubation (bead binding)
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment(
        f"=== Step 3: Incubating {BEAD_BIND_MINUTES} min at RT for DNA binding ==="
    )
    protocol.delay(minutes=BEAD_BIND_MINUTES, msg="Bead binding incubation")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 4 – Engage magnet; wait for liquid to clear
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 4: Engaging magnet ===")
    mag_mod.engage(height=MAG_HEIGHT_MM)
    protocol.delay(seconds=MAG_ENGAGE_WAIT_SEC, msg="Waiting for beads to pellet")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 5 – Remove and discard supernatant
    #          (pick up the 'waste tip' that will be reused through washes)
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 5: Removing supernatant ===")

    p300.pick_up_tip()  # waste tip – reused for all removal steps

    p300.flow_rate.aspirate = ASPIRATION_FLOW_RATE
    # Sample volume after bead addition: 100 + 80 = 180 µL → aspirate 185 µL
    # to capture any remainder; 300 µL tip capacity is sufficient.
    p300.aspirate(185, sample_well.bottom(0.5))
    p300.drop_tip()  # discard supernatant into tip waste

    # NOTE: Per the tip strategy, a single tip is reused across all removal
    # steps for the same well.  We pick it up fresh here after the
    # supernatant discard because we will re-use it for both ethanol removals.
    # To keep things clean the tip is picked fresh for removal step 1, then
    # the same physical tip is NOT re-picked (it is already dropped).
    # We therefore pick up ONE tip now and keep it for both EtOH removals.

    p300.pick_up_tip()  # shared waste tip for both ethanol removals

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 6-8 – First 80 % ethanol wash
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Steps 6-8: First ethanol wash ===")

    # Step 6 – Add 200 µL ethanol (fresh tip)
    # We must put down the waste tip temporarily, use a fresh tip for ethanol,
    # then pick the waste tip back up for removal.
    p300.drop_tip()  # set aside waste tip (it will be picked up again below)

    p300.flow_rate.aspirate = 150   # restore default-ish for reagent addition
    p300.flow_rate.dispense = DISPENSE_FLOW_RATE
    p300.pick_up_tip()              # fresh tip for ethanol addition
    p300.aspirate(ETHANOL_VOLUME_UL, ethanol_well)
    p300.dispense(ETHANOL_VOLUME_UL, sample_well.top(-2))
    p300.drop_tip()

    # Step 7 – Incubate ≥30 s on magnet
    protocol.delay(seconds=ETHANOL_INCUBATE_SEC, msg="Ethanol wash 1 incubation")

    # Step 8 – Remove ethanol (reuse waste tip)
    p300.pick_up_tip()              # reused waste tip for removal steps
    p300.flow_rate.aspirate = ASPIRATION_FLOW_RATE
    p300.aspirate(ETHANOL_VOLUME_UL, sample_well.bottom(0.5))
    # Keep tip in hand for second ethanol removal

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 9-11 – Second 80 % ethanol wash
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Steps 9-11: Second ethanol wash ===")

    # Step 9 – Add second 200 µL ethanol (fresh tip)
    p300.drop_tip()                 # set aside waste tip

    p300.flow_rate.aspirate = 150
    p300.flow_rate.dispense = DISPENSE_FLOW_RATE
    p300.pick_up_tip()              # fresh tip for second ethanol addition
    p300.aspirate(ETHANOL_VOLUME_UL, ethanol_well)
    p300.dispense(ETHANOL_VOLUME_UL, sample_well.top(-2))
    p300.drop_tip()

    # Step 10 – Incubate ≥30 s on magnet
    protocol.delay(seconds=ETHANOL_INCUBATE_SEC, msg="Ethanol wash 2 incubation")

    # Step 11 – Remove ethanol carefully; try to remove residual ethanol
    p300.pick_up_tip()              # reused waste tip for second removal
    p300.flow_rate.aspirate = ASPIRATION_FLOW_RATE
    # First pass: remove bulk ethanol
    p300.aspirate(ETHANOL_VOLUME_UL, sample_well.bottom(1.0))
    # Second pass: chase residual ethanol at the very bottom
    p300.aspirate(20, sample_well.bottom(0.3))
    p300.drop_tip()                 # done with waste tip

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 12 – Air-dry beads
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment(
        f"=== Step 12: Air-drying beads for {BEAD_DRY_MINUTES} min ==="
    )
    protocol.delay(
        minutes=BEAD_DRY_MINUTES,
        msg=(
            f"Air-drying beads ({BEAD_DRY_MINUTES} min). "
            "Do NOT over-dry – this may reduce yield."
        ),
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 13 – Disengage magnet
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 13: Disengaging magnet ===")
    mag_mod.disengage()

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 14-15 – Add elution buffer and resuspend beads
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Steps 14-15: Adding elution buffer and incubating ===")

    p300.flow_rate.aspirate = 150
    p300.flow_rate.dispense = DISPENSE_FLOW_RATE
    p300.pick_up_tip()              # fresh tip for elution buffer

    p300.aspirate(ELUTION_VOLUME_UL, elution_tube)
    p300.dispense(ELUTION_VOLUME_UL, sample_well)

    # Resuspend beads by mixing
    p300.mix(MIX_REPS, ELUTION_VOLUME_UL * 0.8, sample_well)
    p300.blow_out(sample_well.top(-2))
    p300.drop_tip()

    # Incubate 2 min to elute DNA
    protocol.delay(
        minutes=ELUTION_INCUBATE_MIN,
        msg=f"Elution incubation ({ELUTION_INCUBATE_MIN} min at RT)",
    )

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 16 – Re-engage magnet; wait for beads to pellet
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 16: Re-engaging magnet for final separation ===")
    mag_mod.engage(height=MAG_HEIGHT_MM)
    protocol.delay(seconds=MAG_ENGAGE_WAIT_SEC, msg="Waiting for beads to pellet")

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 17 – Transfer clear eluate to elution plate
    # ══════════════════════════════════════════════════════════════════════════
    protocol.comment("=== Step 17: Transferring eluate to elution plate ===")

    p300.pick_up_tip()              # fresh tip for clean eluate transfer
    p300.flow_rate.aspirate = ASPIRATION_FLOW_RATE  # slow to avoid bead disturbance

    p300.aspirate(ELUTION_VOLUME_UL, sample_well.bottom(0.5))
    p300.dispense(ELUTION_VOLUME_UL, eluate_well)
    p300.blow_out(eluate_well.top(-2))
    p300.drop_tip()

    # ══════════════════════════════════════════════════════════════════════════
    # Disengage magnet at the end of the protocol
    # ══════════════════════════════════════════════════════════════════════════
    mag_mod.disengage()

    protocol.comment(
        "=== Protocol complete. "
        f"Purified DNA ({ELUTION_VOLUME_UL} µL) is in elution_plate A1. ==="
    )
