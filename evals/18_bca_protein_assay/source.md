# Source — Thermo Pierce BCA Protein Assay (microplate procedure)

**URL:** https://documents.thermofisher.com/TFS-Assets/LSG/manuals/MAN0011430_Pierce_BCA_Protein_Asy_UG.pdf
**Alternative URL:** https://bio-protocol.org/pdf/bio-protocol44.pdf (Bio-protocol BCA reference)
**Pulled:** 2026-05-29 (via search summarization — the protocol PDF was inaccessible to direct fetch, but the procedural language is canonical and widely reproduced)
**License:** Thermo Pierce product documentation, publicly distributed with kit

The BCA assay is one of the most-run protein quantification workflows in research labs. The Thermo Pierce kit (catalog 23225) and its microplate procedure are the de facto standard reference.

For this case we use **the OT-2-runnable portion only**: BSA standards prep + sample addition + Working Reagent (WR) addition + 37°C incubation. The plate-reader absorbance measurement step is out of scope.

---

## Pierce BCA Microplate Procedure (verbatim)

> Pipette 25 µL of each standard or unknown sample replicate into a microplate well (working range = 20–2000 µg/mL).

> Add 200 µL of the WR to each well and mix plate thoroughly on a plate shaker for 30 seconds.

> Cover the plate and incubate at 37°C for 30 minutes, then measure the absorbance at or near 562 nm on a plate reader.

## Standard curve preparation (canonical convention)

A standard curve is built by serial dilution of a BSA stock (typically 2.0 mg/mL) across 8–9 points spanning 0–2000 µg/mL. Common dilution scheme:
- 2000, 1500, 1000, 750, 500, 250, 125, 25, 0 µg/mL
- Diluent: same as the sample buffer

## Working Reagent (WR) preparation

WR is made fresh by mixing Reagent A and Reagent B at a 50:1 ratio (e.g., 50 mL A + 1 mL B for a full 96-well plate's worth of working reagent).
