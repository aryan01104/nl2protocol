# nl2protocol

Convert natural language instructions into executable [Opentrons](https://opentrons.com/) OT-2 robot protocols using Claude LLM.

Describe your lab procedure in plain English, and nl2protocol generates validated, simulator-tested Python code ready to run on Opentrons OT-2 liquid-handling robots. The system uses a domain-specific reasoning pipeline: the LLM reasons through your protocol (chain-of-thought), produces a structured specification, and deterministic code handles all hardware mapping — so user-specified values like volumes are never silently changed.

> **Status**: Alpha (v0.3.0). 7/8 test cases passing across protocols of varying complexity. See [Roadmap](#roadmap) for planned improvements.

---

## Table of Contents

- [How It Works](#how-it-works)
- [Features](#features)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [CLI Reference](#cli-reference)
- [Configuration](#configuration)
- [Supported Commands](#supported-commands)
- [Architecture](#architecture)
- [Demo Protocols](#demo-protocols)
- [Data Curation (RAG)](#data-curation-rag)
- [Testing](#testing)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## How It Works

Let's walk through what happens when you type an instruction. This shows what the system does well, where it's careful, and where it still has gaps.

### Example: "Distribute 10.5uL of master mix from tube rack A1 to wells A1-H3 of the PCR plate. Transfer 2uL of DNA template. Mix 3 times at 10uL. Use the p20."

**Stage 1 — The system reasons through your instruction (LLM, one call).**

It doesn't jump to code. It thinks first, like a reasoning model:

> "This is a PCR setup. Two steps: distribute master mix, then transfer template with mixing. The user said 10.5uL — that's specific, I'll preserve it exactly. They said 'use the p20' — I'll record that as a hint. Wells A1-H3 means 24 wells in column-major order."

It produces a **ProtocolSpec** — a structured understanding of what you want, in science terms. No Opentrons API names, no slot numbers, no pipette mounts. Just: what action, what substance, what volume, from where, to where.

**Stage 2 — The system checks its own work (deterministic, no LLM).**

- *Hallucination guard*: It regex-extracts every volume from your instruction text (10.5, 2.0, 10.0) and checks that the spec's volumes match. If the LLM invented a volume that isn't in your text, it gets flagged.
- *Sufficiency check*: Does every step have what it needs? Volume? Source? Destination? If not, it tries to fill gaps from the config (e.g., "the reservoir" → find which config labware is a reservoir).
- *Self-refinement*: If gaps remain after filling, it feeds them back to the LLM once: "Step 3 is missing a volume — re-read the instruction." This is one retry for understanding, not for code generation.

**Stage 3 — It shows you what it understood and asks for confirmation.**

```
PROTOCOL SPECIFICATION
  1. DISTRIBUTE 10.5uL of master mix from tube rack well A1
     to PCR plate wells A1-H3
  2. TRANSFER 2.0uL of DNA template from sample plate wells A1-H3
     to PCR plate wells A1-H3
     → mix 3x at 10.0uL
     → pipette hint: p20
  
  Locked volumes (from instruction): [2.0, 10.0, 10.5]

  Does this look right? [Y/n]
```

You can say no and rephrase. The "locked volumes" line tells you which values came directly from your text — the system will never change these.

**Stage 4 — Deterministic conversion to robot instructions (no LLM).**

`spec_to_schema()` maps science to hardware using your lab config:
- "tube rack" → scores against config labels → best match: `reagent_rack` (slot 2)
- 10.5uL → scans pipette ranges → p20 (1-20uL) fits, p300 (20-300uL) doesn't → assigns left mount
- "A1-H3" → expands to [A1, B1, C1, ..., H3] (24 wells, column-major)

This is where the earlier bug was fixed. The old system asked the LLM to make these hardware decisions. When a volume didn't fit a pipette, the retry loop would change the volume (10.5 → 20.0) instead of switching the pipette. Now the volume is copied directly — no LLM can touch it — and the pipette is selected deterministically from the range.

**Stage 5 — Code generation (deterministic, no LLM).**

`ProtocolSchema` → Python script. Same schema always produces the same code. This was already working well in the earlier architecture.

**Stage 6 — Simulation and verification.**

The Python runs through the Opentrons simulator (catches tip errors, invalid wells, API mistakes). Then an LLM checks intent: "does this protocol match what the user asked?" as a safety net.

### What it can't do yet

**It doesn't ask you about missing parameters.** If you say "do a serial dilution" without specifying a volume, the LLM infers 100uL from domain knowledge and tags it as `(inferred)`. You see the tag in the confirmation step, but the system doesn't proactively ask: "what volume did you want? typical is 100uL." This is the next feature to build.

**Complex well layouts can trip it up.** "Drug 1 goes to columns 1-3 in triplicate, Drug 2 goes to columns 4-6" requires understanding that each drug's 8 concentrations map across replicate columns. The well range parser handles standard formats but not every natural language description.

**It can run out of tips.** Big protocols (6 drugs × 8 concentrations × triplicate) need more tip changes than the configured tipracks can provide. The simulator correctly catches this, but the system doesn't suggest "add more tipracks to your config."

### Where values come from

Every value in the generated protocol has one of three origins:

1. **From your instruction** — volumes, wells, substances you explicitly wrote. These are ground truth and are never modified. Tagged as explicit.
2. **From domain knowledge (LLM)** — when you say "serial dilution" without specifying volume, the LLM may infer 100uL as typical. Tagged as `(inferred)` so you can see it and override.
3. **From your config** — labware API names, deck slots, pipette mounts, tiprack assignments. These are facts about your physical setup, looked up deterministically.

```
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 1: REASONING (LLM — one call)                            │
│                                                                  │
│  The LLM thinks through the protocol step by step:              │
│  "This is a PCR setup. It has 2 steps. 10.5uL needs the p20."  │
│                                                                  │
│  Input:  Natural language instruction + lab config               │
│  Output: ProtocolSpec (structured JSON — the "science language") │
│          Actions, substances, volumes, wells, tip strategy       │
│          Volumes tagged as explicit / approximate / inferred     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 2: VALIDATION (deterministic — no LLM)                   │
│                                                                  │
│  Hallucination guard: do extracted volumes match instruction?    │
│  Sufficiency check: does every step have what it needs?          │
│  Gap filling: infer missing sources from config contents         │
│  Self-refinement: if gaps remain, ask LLM to try once more      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 3: USER CONFIRMATION                                     │
│                                                                  │
│  "Here's what I understood:                                      │
│   1. DISTRIBUTE 10.5uL of master mix...                          │
│   2. TRANSFER 2uL of DNA template...                             │
│   Does this look right? [Y/n]"                                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 4: DETERMINISTIC CONVERSION (no LLM)                     │
│                                                                  │
│  spec_to_schema(): Maps science → hardware                      │
│    "tube rack" → reagent_rack, slot 2                            │
│    10.5uL → p20 pipette on left mount                            │
│    "A1-H3" → [A1, B1, C1, ..., H3]                              │
│                                                                  │
│  Volumes are COPIED, never re-interpreted.                       │
│  Pipette selection is deterministic (volume → range match).      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 5: CODE GENERATION (deterministic — no LLM)              │
│  ProtocolSchema → Python script (Opentrons API v2.15)           │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 6: SIMULATION + VERIFICATION                              │
│  Opentrons simulator (catches hardware errors)                   │
│  + LLM intent verification (safety net, ≥70% confidence)        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
                        working_protocol.py
```

**Why this architecture?** An earlier version used a single LLM call to generate the full robot schema directly, with a retry loop for errors. We found that the retry loop could silently corrupt user-specified values — for example, changing a volume from 10.5uL to 20.0uL to fit a wrong pipette's range. By separating understanding (LLM) from hardware mapping (deterministic code), user-specified values flow through untouched. The LLM reasons about science; the code handles engineering.

---

## Features

- **Natural Language Input** — Describe experiments in plain English, or pass a `.txt` / `.pdf` file.
- **Chain-of-Thought Reasoning** — The LLM thinks through your protocol step by step before producing structured output, handling both simple instructions and named protocols (e.g., "do the Bradford assay").
- **User Confirmation** — Shows you exactly what it understood before generating code. Every value is tagged as explicit (from your text), inferred (from domain knowledge), or from config.
- **Deterministic Hardware Mapping** — Pipette selection, labware resolution, and well expansion are all deterministic code. User-specified volumes are never re-interpreted by an LLM.
- **Hallucination Guard** — Regex-extracted volumes from your instruction are cross-checked against the spec. Invented values get flagged.
- **Hardware Module Support** — Temperature, magnetic, heater-shaker, and thermocycler modules.
- **Simulator-Validated** — Every protocol passes the Opentrons simulation before output.
- **Intent Verification** — A second LLM pass confirms the generated protocol matches what you asked for.
- **Auto-Config Generation** — `--generate-config` infers labware, pipettes, and modules from your instruction.
- **Robot Upload** — Push protocols directly to your OT-2 via HTTP API, or use demo mode.

---

## Installation

### Prerequisites

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/) (Claude)

### Setup

```bash
# Clone the repository
git clone https://github.com/yourusername/nl2protocol.git
cd nl2protocol

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package (editable)
pip install -e .

# Set up API key (pick one)
export ANTHROPIC_API_KEY="your_api_key_here"
# — or —
echo "ANTHROPIC_API_KEY=your_api_key_here" > .env
```

---

## Quick Start

### Option 1: Auto-generate config (easiest)

```bash
nl2protocol -i "Transfer 100uL from source plate A1 to dest plate B1" --generate-config
```

The system infers required equipment and asks you to confirm before proceeding.

### Option 2: Use an existing lab config

```bash
# Copy the example config and edit to match your deck
cp nl2protocol/config_examples/lab_config.example.json lab_config.json

nl2protocol -i "Transfer 100uL from plate A1 to B1" -c lab_config.json
```

### Option 3: Python API

```python
from nl2protocol import ProtocolAgent

agent = ProtocolAgent(config_path="lab_config.json")
result = agent.run_pipeline(
    prompt="Transfer 50uL from wells A1-A8 of the source plate to dest plate"
)

if result:
    print(result.script)
    with open("my_protocol.py", "w") as f:
        f.write(result.script)
```

---

## CLI Reference

```
nl2protocol [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `-i`, `--instruction` | Instruction text, or path to `.txt` / `.pdf` file |
| `-c`, `--config` | Path to lab config JSON |
| `--generate-config` | Auto-generate config from instruction |
| `-d`, `--data` | Path to CSV data file |
| `-o`, `--output` | Output file path (default: timestamped `generated_protocol_*.py`) |
| `--robot` | Upload to OT-2 after successful generation |
| `--validate-only` | Validate config file and exit |
| `--help` | Show help |

### Examples

```bash
# Read instruction from file
nl2protocol -i protocol.txt -c lab_config.json

# With CSV data
nl2protocol -i "Distribute samples per data file" -c lab.json -d samples.csv

# Upload to robot after generation
nl2protocol -i "Mix wells A1-A6" -c lab.json --robot

# Validate config file only
nl2protocol --validate-only -c lab_config.json
```

---

## Configuration

### Lab Config (`lab_config.json`)

Describes your physical deck layout — labware, pipettes, and optional hardware modules.

```json
{
    "labware": {
        "tiprack": {
            "load_name": "opentrons_96_tiprack_300ul",
            "slot": "1"
        },
        "source_plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "2",
            "contents": { "A1": "Sample A", "A2": "Sample B" }
        },
        "dest_plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "3"
        }
    },
    "pipettes": {
        "left": {
            "model": "p300_single_gen2",
            "tipracks": ["tiprack"]
        }
    },
    "modules": {
        "temp_mod": {
            "module_type": "temperature",
            "slot": "6"
        }
    }
}
```

See `nl2protocol/config_examples/lab_config.example.json` for a full example with modules.

### Robot Config (`robot_config.json`)

```json
{
    "robot_ip": "192.168.1.100",
    "robot_name": "My OT-2",
    "demo_mode": true
}
```

Set `demo_mode: false` and update `robot_ip` to connect to a real robot. The robot and your machine must be on the same network. Find your robot's IP on its touchscreen under Settings > Network.

---

## Supported Commands

### Liquid Handling

| Command | Description |
|---------|-------------|
| `aspirate` | Draw liquid into pipette tip |
| `dispense` | Push liquid out of pipette tip |
| `mix` | Aspirate/dispense cycles to mix |
| `transfer` | Move liquid from source to destination |
| `distribute` | One source to multiple destinations |
| `consolidate` | Multiple sources to one destination |
| `blow_out` | Expel remaining liquid/air |
| `touch_tip` | Touch tip to well sides |
| `air_gap` | Add air gap to prevent dripping |
| `pick_up_tip` / `drop_tip` | Manual tip management |

### Flow Control

| Command | Description |
|---------|-------------|
| `pause` | Pause for user intervention |
| `delay` | Wait for specified time |
| `comment` | Add comment to run log |

### Hardware Modules

| Module | Commands |
|--------|----------|
| Temperature | `set_temperature`, `wait_for_temperature`, `deactivate` |
| Magnetic | `engage_magnets`, `disengage_magnets` |
| Heater-Shaker | `set_shake_speed`, `open_latch`, `close_latch` |
| Thermocycler | `set_block_temperature`, `set_lid_temperature`, `open_lid`, `close_lid`, `run_profile` |

---

## Architecture

### Two languages, one pipeline

The system has two representations of a protocol, and a deterministic bridge between them:

**ProtocolSpec** (`extractor.py`) — the "science language." Actions, substances, volumes, wells, tip strategy. No hardware details. This is what the LLM produces. A scientist can read it and say "yes, that's what I want."

**ProtocolSchema** (`models.py`) — the "hardware language." Opentrons API labware names, deck slots, pipette mounts, command objects. This is what gets converted to Python code. A robot can execute it.

**`spec_to_schema()`** (`extractor.py`) — the deterministic bridge. Maps "tube rack" → `reagent_rack` on slot 2. Maps 10.5uL → p20 on left mount. Expands "A1-H3" → 24 wells. No LLM involved, so values can't be corrupted.

### Project Structure

```
nl2protocol/
├── nl2protocol/
│   ├── extractor.py          # ProtocolSpec + reasoning + spec_to_schema()
│   ├── app.py                # Pipeline orchestration + code generation + simulation
│   ├── models.py             # ProtocolSchema + hardware-level validation
│   ├── parser.py             # Config loading + well info enrichment
│   ├── cli.py                # Command-line interface
│   ├── robot.py              # OT-2 HTTP API client
│   ├── config_generator.py   # Auto-generate lab config from instruction
│   ├── input_validator.py    # Classify input (protocol / question / invalid)
│   ├── example_store.py      # RAG vector store (ChromaDB + sentence-transformers)
│   ├── validate_config.py    # Lab config JSON validation
│   ├── errors.py             # Custom exceptions
│   ├── examples/             # 238 RAG training examples
│   └── config_examples/      # Sample lab configs
│
├── test_cases/               # 8 test protocols (instruction.txt + config.json)
├── demo_protocols/           # 5 demo protocols ready to run
├── notes/                    # Architecture notes and design docs
├── pyproject.toml            # Package metadata and dependencies
└── requirements.txt          # Python dependencies
```

### Key Design Decisions

1. **LLM reasons, code maps.** The LLM's only job is to understand the instruction and produce a structured spec. All hardware decisions (which pipette, which slot, which API name) are deterministic code. This is why user-specified volumes can never be silently changed — they never pass through an LLM after extraction.

2. **Chain-of-thought reasoning.** The extraction prompt asks the LLM to think step by step before producing structured output (inspired by [Raschka, Ch. 4](https://www.manning.com/books/build-a-reasoning-model-from-scratch) on inference-time compute scaling). This handles both simple instructions ("transfer 100uL") and complex ones ("do the Bradford assay") with the same call — the reasoning adapts to complexity.

3. **Pydantic validation at the boundary.** `ProtocolSchema` enforces that labware exists in config, volumes are within pipette range, and all references are valid. Error messages now suggest which pipette to switch to (not just "volume out of range"), guiding self-correction toward the right fix.

4. **Three sources of truth.** Every value in the output has a clear origin: from your instruction (explicit, locked), from domain knowledge (inferred, shown to you), or from your config (deterministic lookup). You can trace any value back to why it's there.

---

## Demo Protocols

Five common biology lab protocols are included for demonstration:

| Protocol | Key Feature |
|----------|-------------|
| Bradford Assay | Protein quantification with BSA standard curve |
| Bacterial Transformation | Heat shock with temperature module |
| Plasmid Miniprep | Magnetic bead DNA extraction |
| Cell Seeding | Auto-config generation demo |
| Western Blot Prep | Temperature-controlled sample preparation |

```bash
# Example: run the Bradford Assay demo
nl2protocol \
  -i demo_protocols/bradford_assay/instruction.txt \
  -c demo_protocols/bradford_assay/config.json \
  -o bradford_protocol.py
```

See [demo_protocols/README.md](./demo_protocols/README.md) for all 5 demos and robot configuration details.

---

## Data Curation (RAG)

The `data_curation/` directory contains scripts that reverse-engineer existing Opentrons protocol scripts into (intent, schema) training pairs for the RAG system:

1. **`script_parser.py`** — AST-based parser extracts labware, pipettes, and commands from Python scripts.
2. **`intent_generator.py`** — LLM generates 3 natural-language descriptions per script (technical, casual, concise).
3. **`run_pipeline.py`** — Orchestrates the full curation pipeline.

Each script produces 3 examples, stored as JSON in `nl2protocol/examples/`.

See [data_curation/README.md](./data_curation/README.md) for usage instructions.

---

## Testing

### Test Cases

10 protocols of varying complexity in `test_cases/`, each with `instruction.txt` and `config.json`:

- `simple_transfer` — Basic A1→B1 transfer
- `distribute` — One source to multiple destinations
- `serial_dilution` — 2x serial dilution across row A
- `pcr_mastermix` — PCR master mix setup
- `magnetic_bead_cleanup` — Magnetic module bead extraction
- `cell_viability_assay` — Cell viability testing
- `qpcr_standard_curve` — qPCR standard curve setup
- `elisa_sample_addition` — ELISA sample addition

### Current Results (7/8 passing)

| Test | Result | Notes |
|------|--------|-------|
| simple_transfer | PASS | Basic 3-well transfer |
| pcr_mastermix | PASS | The bug case: 10.5uL preserved, p20 correctly assigned |
| distribute | PASS | Single-channel correctly selected |
| serial_dilution | PASS | 11 sequential transfers with mixing |
| elisa_sample_addition | PASS | Serial dilution + duplicate plating |
| magnetic_bead_cleanup | PASS | Magnetic module + multi-step wash |
| qpcr_standard_curve | PASS | Temperature module + serial dilution + triplicates |
| cell_viability_assay | FAIL | OutOfTipsError — protocol needs more tipracks than config provides (real physical constraint, not a code bug) |

---

## Roadmap

### Next: Interactive parameter filling

When you say "do a serial dilution" without specifying a volume, the system currently infers a default (100uL) and tags it as `(inferred)`. Better: it should ask you, with a reasoned suggestion:

> "You didn't specify a transfer volume. For a 2-fold serial dilution in a 360uL plate, 100uL is standard because it gives a 1:2 ratio and stays well under plate capacity. Press enter to accept, or type a different value:"

The architecture already supports this — the sufficiency check identifies gaps, the LLM has domain knowledge for suggestions, and the confirmation step already pauses for input. The missing piece is turning auto-fill into a question.

### Planned

- [ ] **Self-consistency sampling** — Generate 2-3 ProtocolSpecs, compare them, pick the consensus. Catches reasoning errors through majority voting ([Raschka, Ch. 4](https://www.manning.com/books/build-a-reasoning-model-from-scratch)).

- [ ] **Simulation error feedback** — When the simulator fails, feed the error back to the reasoning step for re-extraction instead of just stopping (self-refinement, [Raschka, Ch. 5](https://www.manning.com/books/build-a-reasoning-model-from-scratch)).

- [ ] **GUI** — Web UI showing the spec as an editable form: volumes in input fields, wells on a plate diagram, pipette as a dropdown. ProtocolSpec is just JSON, so this is a frontend problem.

- [ ] **Protocol template library** — Pre-built ProtocolSpec templates for common protocols (PCR, serial dilution, ELISA) with standard parameters. Reduces LLM dependence for well-known procedures.

- [ ] **Tip budget awareness** — Before generating, count expected tip usage and warn if it exceeds available tipracks. Would have caught the cell_viability_assay failure before simulation.

- [ ] **Opentrons Flex support** — Extend beyond OT-2 to support the newer Opentrons Flex platform.

---

## Example Instructions

Here are some example natural language instructions the system can handle:

**Serial Dilution**
```
Perform a 2x serial dilution across row A. Start with 200uL stock in A1.
Transfer 100uL from A1 to A2, mix, then A2 to A3, and so on through A12.
```

**Plate Replication**
```
Copy 50uL from each well in column 1 of the source plate to the corresponding
wells in column 1 of the destination plate. Use a new tip for each transfer.
```

**Temperature-Controlled Transfer**
```
Set the temperature module to 4C and wait until it reaches temperature.
Then transfer 20uL from each sample tube to the cold plate, using a new tip each time.
```

**PCR Setup**
```
Distribute 10uL of master mix from tube rack A1 to all wells of a 96-well PCR plate.
Then add 2uL of template DNA from wells A1-H1 of the source plate to the
corresponding rows of the PCR plate.
```

---

## Contributing

This project is in alpha. Contributions are welcome — particularly around:
- Additional RAG training examples (especially module-heavy protocols)
- Labware load name validation
- Test coverage
- Documentation improvements

Please open an issue to discuss before submitting large changes.

---

## License

MIT
