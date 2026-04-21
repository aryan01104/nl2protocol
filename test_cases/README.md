# Test Cases

## Examples — Start Here

Working protocols to try. Each has `instruction.txt` + `config.json`:

```bash
python -m nl2protocol -i test_cases/examples/<name>/instruction.txt -c test_cases/examples/<name>/config.json
```

| Example | Description | What it demonstrates |
|---------|-------------|----------------------|
| `simple_transfer` | Transfer between two plates | Basic pipeline, start here |
| `distribute` | Reservoir to plate column | Single source, multiple destinations |
| `serial_dilution` | 2x dilution across row A | Serial dilution chain with mixing |
| `pcr_mastermix` | PCR plate setup | Master mix + template, two pipettes |
| `bradford_assay` | Protein quantification | BSA standard curve + Bradford dye |
| `qpcr_standard_curve` | qPCR with standard curve | Serial dilution + triplicates + temp module |
| `elisa_sample_addition` | ELISA sample plating | Serial dilution + duplicate plating |
| `magnetic_bead_cleanup` | Magnetic bead DNA extraction | Magnetic module, multi-step wash |
| `bacterial_transformation` | Heat shock transformation | Temperature module, timed steps |
| `plasmid_miniprep` | Magnetic bead plasmid extraction | Magnetic module, ethanol washes |
| `cell_seeding` | Seed cells into plate | Simple distribute |
| `cell_viability_assay` | Cell viability testing | Large protocol, tip budget |
| `western_blot_prep` | SDS-PAGE sample prep | Temperature module, denaturation |

## Failure Modes — Designed to Fail

These trigger specific error paths to test constraint checking, validation, and error messages:

```bash
python -m nl2protocol -i test_cases/failure_modes/<name>/instruction.txt -c test_cases/failure_modes/<name>/config.json
```

| Case | What it tests |
|------|---------------|
| `pipette_insufficient` | 500uL transfer with only a p20 |
| `labware_missing` | 384-well plate not in config |
| `module_missing` | Temperature command without temp module |
| `combined_config_gaps` | Multiple missing items at once |
| `nonsensical_instruction` | Centrifuge + absorbance (not liquid handling) |
| `mismatched_protocol` | Instruction vs config for different experiments |
| `wrong_loadname` | Invalid Opentrons load_names in config |
| `misspelled_instruction` | Heavily misspelled instruction text |
| `equivalent_names` | "Eppendorf tubes" vs config "tube_rack" |
| `primed_wells` | Wells already contain liquid |
| `complex_instruction` | Multi-step ELISA with washes and incubation |
| `compact_instruction` | One-line Bradford assay |

## Automated Tests

32 deterministic pytest tests (no API key needed):

```bash
python -m pytest tests/ -v
```
