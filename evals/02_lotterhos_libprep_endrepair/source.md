# Source — Lotterhos Lab Illumina Library Prep (End Repair & A-tailing sub-step)

**URL:** https://drk-lo.github.io/lotterhoslabprotocols/molecprot_illuminalibraryprep.html
**Lab:** Lotterhos Lab (marine genomics, Northeastern University)
**Pulled:** 2026-05-29
**Kit:** KAPA Hyper Prep Kit (KK8505), half reaction volumes
**License:** site publicly published; protocol derivative of Kapa Hyper Prep user guide

The full Lotterhos page is multi-stage (End Repair → Adapter Ligation → Post-Ligation Cleanup). For case 02 we use **only End Repair & A-tailing** — sub-steps 1.1 – 1.4. The cleanup stage would duplicate coverage from case 01; the adapter ligation stage is a distinct reaction shape that could become a separate future case if needed.

The page also notes "PCR, post-PCR cleanup, and tapestation quality control sections are referenced but incomplete in source protocol" — out of our concern.

---

## Overview (from source page)

This protocol describes genomic library preparation using the KAPA Hyper Prep Kit, performed at half the recommended reaction volumes. Before starting, determine your barcode and index scheme.

## Kit and Materials

**Kit:** KAPA Hyper Prep Kit (KK8505)

**Required materials:**
- 500 ng DNA in 25 µL RNase-free water, sheared appropriately
- i5 and i7 primers at 15 µM
- Annealed custom adapters at 15 µM
- KAPA beads

**Equipment:** Thermomixer (20°C, 400 rpm capable), thermocycler, magnetic bead rack

**Consumables:** Filtered 10 µL and 20-200 µL tips; strip or individual tubes

---

## Protocol Steps

### End Repair & A-tailing

**1.1 Reaction assembly:**

Combine fragmented DNA (25 µL), End Repair & A-tailing Buffer (3.5 µL), and End Repair & A-tailing Enzyme (1.5 µL) for 30 µL total. Master mixes acceptable with 10% overage for pipetting error.

**1.2** Vortex gently, spin briefly, return to ice. Proceed immediately.

**1.3 Thermocycler protocol:**
- 20°C for 30 minutes
- 65°C for 30 minutes
- 4°C hold

**1.4** Proceed immediately to adapter ligation.

### Adapter Ligation (NOT in this eval — kept here for context)

**2.1 Reaction assembly:** To end repair product (30 µL), add adapter stock (2.5 µL), PCR-grade water (2.5 µL), Ligation Buffer (15 µL), and DNA Ligase (5 µL) for 55 µL total. Master mixes acceptable with 10% overage.
**2.2** Mix by pipetting 15 times, centrifuge briefly.
**2.3** Incubate at 20°C for 60 minutes with 400 rpm rotation on thermomixer.
**2.4** Proceed immediately to cleanup.

### Post-Ligation Cleanup (NOT in this eval — covered by case 01 pattern)

**3.1 Bead cleanup (0.8X ratio):** Combine adapter ligation product (55 µL) with KAPA Pure Beads (44 µL) for 99 µL total.
**3.2** Mix thoroughly by pipetting 15 times.
**3.3** Incubate at room temperature for 10 minutes to bind DNA.
