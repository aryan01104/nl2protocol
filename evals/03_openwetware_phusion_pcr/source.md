# Source — OpenWetWare Silver Lab PCR (Phusion condition)

**URL:** https://openwetware.org/wiki/Silver:_PCR
**Lab:** Silver Lab (originally MIT/Harvard, contributor to OpenWetWare)
**Pulled:** 2026-05-29
**License:** OpenWetWare content is CC-BY-SA

The Silver lab PCR page documents three polymerase-condition recipes (Vent, Pfx, Phusion). Each is a complete mastermix recipe + cycling program. For case 03 we use **only Condition C (Phusion Polymerase)** — most widely used in modern labs, cleanest 7-reagent assembly.

A real user with this page open and Phusion in their freezer would copy only Condition C, not all three. So slicing here matches realistic user behavior.

---

## Full page transcription (all three conditions)

### Condition A: Vent Polymerase

**Setup Steps:**
1. Resuspend each primer in Tris buffer pH 8.0 or distilled water to 100 µM.
2. Combine the following components:

**Reaction Mix (50 µL total):**
- 5 µL 10x ThermoPol buffer
- 0.4 µL 25 mM dNTPs
- 0.5 µL 100 µM forward primer
- 0.5 µL 100 µM reverse primer
- ≤1 µL plasmid DNA or 2 µL genomic DNA
- 1 µL Vent DNA polymerase
- distilled water to 50 µL total volume

**PCR Program:**
- Start: 95 °C for 2 min (melt)
- Cycle: 95 °C for 0.5 min (melt)
- Tm minus 5 °C for 0.5 min (anneal)
- 74 °C for (# bp/1000) min (extension, minimum 0.5 min)
- 30 cycles total
- End: 4 °C indefinitely

### Condition B: Pfx Polymerase

**Setup Steps:**
1. Resuspend each primer in Tris buffer pH 8.0 or distilled water to 100 µM.
2. Use Stratagene's Pfx kit.
3. Combine components:

**Reaction Mix (100 µL total):**
- 3 µL primer mix (10µM each)
- 0.8 µL template DNA
- 25 µL 10X PFx amplification buffer
- 3 µL 10mM dNTPs
- 2 µL 50mM MgSO4
- 30 µL 10X PFx enhancer buffer
- 34.2 µL water
- 2 µL PFx DNA polymerase

**PCR Program:**
- Start: 94 °C for 5 min (melt)
- Cycle: 94 °C for 15 sec (melt)
- 55 °C for 0.5 min (anneal)
- 68 °C for 3.5 min (extension)
- 68 °C for 7 min
- 35 cycles total
- End: 4 °C indefinitely

### Condition C: Phusion Polymerase  *(this case uses this section only)*

**Setup Steps:**
1. Use NEB Phusion Polymerase Kit
2. Combine components:

**Reaction Mix (50 µL total):**
- 10 µL Phusion Buffer
- 1 µL 10mM dNTPs
- 2.5 µL Forward Primer (10 µM)
- 2.5 µL Reverse Primer (10 µM)
- 2 µL Genomic Template (less for plasmid)
- 0.5 µL Phusion polymerase
- 31.5 µL Distilled Water

**PCR Program:**
- Start: 98 °C for 30 sec
- Cycle: 98 °C for 10 sec
- 45-72 °C for 15 sec (3 degrees above primer Tm)
- 72 °C for 15-30 sec/kb
- 25-35 cycles total
- 72 °C for 5-10 min
- End: 4 °C indefinitely
