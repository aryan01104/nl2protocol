# Source — Lotterhos Lab Plate DNA Extraction (Qiagen DNeasy 96)

**URL:** https://drk-lo.github.io/lotterhoslabprotocols/molecprot_dnaextractionqiagenplate.html
**Lab:** Lotterhos Lab (marine genomics, Northeastern University)
**Pulled:** 2026-05-29
**License:** site publicly published

**Note on the original "(bead)" label in the matrix:** Lotterhos doesn't have a pure magnetic-bead DNA extraction protocol — only column-based (Omega and Qiagen). The Qiagen plate protocol is **hybrid**: OT-2 handles the liquid handling (buffer additions to 96-well plate), the user manually transfers the plate to a centrifuge between OT-2 stages. This is a real and common workflow pattern — and it tests a different shape than magbead cleanup (case 01): explicit user-manual breaks inside what's otherwise an automated pipeline.

For this case we use **only the EXTRACTION section** (the OT-2-shaped portion). The upstream TISSUE LYSING section is almost entirely manual (tissue weighing, scalpel work, ethanol sterilization) and out of scope.

---

## EXTRACTION section (verbatim from source)

1. Prep surfaces and area around the work bench. First clean with bleach solution, followed by DI H2O, then 70% EtoH.

2. Remove the entire sample tube rack from the Thermomixer and ensure microtubes in the tube rack are properly sealed with their individual caps. Cover again with clear plastic lid and shake the tube rack vigorously by hand for 15 seconds. Then secure the lid to the rack with lab tape. Centrifuge in the shared molecular space centrifuge (45s at 4400 rpm) to collect any solution from the caps. Hold down short spin button.

3. Use 25 mL serological pipette tips with the Easy-Pet pipette to transfer 45 mL Buffer AL to a 50mL liquid boat (enough for a full plate of samples). Carefully remove sample caps and add 410 μl Buffer AL using the 1000μL multichannel pipette (only go to the first stop to avoid bubbles). If you are careful not to touch the pipette tip to the sample tubes or another contaminated surface, you can put the same tip into the buffer multiple times to transfer the full volume. Tightly reseal with new caps.

4. Place clear cover over the rack and shake vigorously for 15s by hand. Secure the lid to the rack with lab tape, then centrifuge in the shared lab space centrifuge (45s at 4400 rpm) to collect any solution from the caps. Make sure to add water to the counterweight.

5. Place DNeasy 96 plate on the S-Block. Make sure well A1 is directly on top of well A1, etc. Note that the DNeasy plate on the S-block is very prone to tipping, especially when applying tape sheets. Be careful not to let them tip or lysate may slosh out and cause contamination between samples.

6. Carefully remove caps from the collection microtubes & transfer all lysate of each sample to the corresponding wells on the DNeasy 96 plate using filtered tips. Set the P1000 multichannel pipette to 600 uL, and transfer all lysate in as many rounds as necessary to get as much as possible into the DNeasy plate. The lysate can get super bubbly, so be careful not to get any sample up into the actual pipette. There's usually about 600 uL total lysate in each sample. Pop any bubbles using a pipette tip. Dispose of empty collection microtubes and blue tube rack in the mayo jar.

7. Seal the plate with an AirPore tape sheet which comes with the Qiagen kit, then secure the DNeasy plate to the S-block with lab tape. Centrifuge in the shared molecular space centrifuge (13 min at 4500 rpm).

8. Remove tape and carefully add 500μl of Buffer AW1 to each sample. AW1 is harmful if ingested or inhaled (acutely toxic), and can cause skin and eye irritation. Use the 1000μl multichannel pipette. This will require ~55mL of Buffer AW1 in a liquid boat.

9. Seal the plate with a new AirPore tape sheet. Secure the DNeasy plate to the S-block with lab tape, and centrifuge in the shared molecular space (7 min at 4500 rpm).

10. Remove tape and carefully add 500μl of Buffer AW2 to each sample. Use the 1000μl multichannel pipette. This will require ~55mL of Buffer AW2 in a liquid boat.

11. Seal the plate with a new AirPore tape sheet and centrifuge at 4500 rpm for 20 min in the shared molecular space.

12. Transfer the DNeasy 96 plate to a rack of Elution Microtubes RS.

13. Elute the DNA by adding 200μl of Low TE Buffer OR molecular grade DiH2O to each sample using the 1000μl multichannel pipette. We have found Low TE Buffer gives better yields than kit Buffer AE. For a full plate, this will require ~20mL of low TE OR molecular grade DiH2O. Seal with a new AirPore tape sheet & incubate for 10 min at room temperature (15–25°C). Secure the DNeasy plate to the Elution Microtubes with lab tape, and centrifuge in the shared lab space centrifuge (4 min at 4400 rpm). Seal elution microtubes with new caps.

14. Add the elution buffer data to the app/database.

15. Label plate and extracted DNA can be placed in "No animals" fridge for a few days, or store at -20 or -80 for long term storage. AVOID numerous freeze-thaw cycles. Record time samples were put into the fridge/freezer in the app/datasheet.
