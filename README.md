# nl2protocol

Convert natural language instructions into executable [Opentrons](https://opentrons.com/) OT-2 robot protocols using Claude LLM.

Describe your lab procedure in plain English, and nl2protocol generates validated, simulator-tested Python code ready to run on Opentrons OT-2 liquid-handling robots. The system uses retrieval-augmented generation (RAG) for few-shot learning, a structured intermediate schema for deterministic code generation, and a self-correcting retry loop backed by the Opentrons simulator.

> **Status**: Alpha (v0.2.0). Core pipeline works end-to-end. See [Roadmap](#roadmap) for known gaps.

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

```
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 1: REASONING                                              │
│  Natural language + Lab config → ProtocolSchema (JSON)           │
│  Claude Sonnet · RAG few-shot examples · Pydantic validation     │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 2: GENERATION                                             │
│  ProtocolSchema → Executable Python script (Opentrons API v2.15)│
│  Deterministic code generation — same schema always = same code │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  LAYER 3: VERIFICATION                                           │
│  Opentrons simulator (catches hardware errors)                   │
│  + LLM intent verification (≥70% confidence threshold)          │
└──────────────────────────────────────────────────────────────────┘
                              ↓
                    ┌────────────────┐
                    │ Pass? → Output │
                    │ Fail? → Retry  │←─────┐
                    └────────────────┘      │
                              │             │
                              └─────────────┘
                           (up to 3 retries)
```

The three-layer design keeps LLM reasoning (non-deterministic) separate from code generation (deterministic). The retry loop feeds simulation errors back to the LLM so it can self-correct.

---

## Features

- **Natural Language Input** — Describe experiments in plain English, or pass a `.txt` / `.pdf` file.
- **Auto-Config Generation** — `--generate-config` infers labware, pipettes, and modules from your instruction.
- **RAG Few-Shot Learning** — 238 curated examples in a ChromaDB vector store improve output quality.
- **CSV Data Integration** — Import well maps and experimental parameters from spreadsheets.
- **Hardware Module Support** — Temperature, magnetic, heater-shaker, and thermocycler modules.
- **Simulator-Validated** — Every protocol passes the Opentrons simulation before output.
- **Self-Correcting** — Errors are fed back to the LLM for automatic retry (up to 3 attempts).
- **Intent Verification** — A second LLM pass confirms the generated protocol matches what you asked for.
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

### Project Structure

```
nl2protocol/
├── nl2protocol/              # Main package
│   ├── app.py                # Pipeline orchestration + code generation
│   ├── parser.py             # LLM reasoning (NL + config → ProtocolSchema)
│   ├── models.py             # Pydantic schemas (ProtocolSchema, all command types)
│   ├── cli.py                # Command-line interface
│   ├── robot.py              # OT-2 HTTP API client (upload, run, status)
│   ├── config_generator.py   # Auto-generate lab config from NL instruction
│   ├── input_validator.py    # Classify input (protocol vs. question vs. invalid)
│   ├── example_store.py      # RAG vector store (ChromaDB + sentence-transformers)
│   ├── validate_config.py    # Lab config JSON validation
│   ├── errors.py             # Custom exception types
│   ├── examples/             # 238 RAG training examples (JSON)
│   └── config_examples/      # Sample lab configs
│
├── test_cases/               # 10 test protocols (instruction.txt + config.json)
├── demo_protocols/           # 5 demo protocols ready to run
├── data_curation/            # Scripts to generate RAG examples from existing protocols
├── pyproject.toml            # Package metadata and dependencies
└── requirements.txt          # Python dependencies
```

### Key Design Decisions

1. **Three-layer separation** — LLM reasoning produces a structured JSON schema; code generation from that schema is deterministic. This means simulation failures can be traced to either the LLM's understanding (Layer 1) or the code generator (Layer 2) independently.

2. **Pydantic validation at the boundary** — `ProtocolSchema` enforces that labware references exist in the config, volumes are within pipette capacity, well names are valid for the plate type, and temperatures are within module ranges. This catches hallucinations before code generation.

3. **RAG for few-shot examples** — ChromaDB + `all-MiniLM-L6-v2` embeddings find the 3 most similar protocols from a curated set of 238 examples. This grounds the LLM's output in real, working protocol patterns.

4. **Self-correcting retry loop** — Simulation errors and failed JSON outputs are fed back to the LLM on subsequent attempts, giving it specific context about what went wrong.

For a detailed technical breakdown of the pipeline, see [4human.md](./4human.md).

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

### What's Tested

- **Layer 2 (code generation)** — Snapshot/golden file tests ensure `generate_python_script()` produces correct output for known schemas.
- **Simulation** — All generated code is run through the Opentrons simulator.

### Not Yet Tested

- Layer 1 (LLM parsing) — non-deterministic, hard to test with traditional methods
- End-to-end (NL → robot) — requires integration test infrastructure

---

## Roadmap

### Known Limitations & Planned Improvements

- [ ] **Config validation for generated/initial files** — Verify that the initial generated protocol (or a non-generated protocol file) is consistent with the lab config before simulation. Currently, config mismatches are only caught at simulation time, which wastes a retry cycle.

- [ ] **Stricter hallucination guardrails** — The LLM can still hallucinate labware load names, invalid well references, or unsupported command sequences. Planned improvements include:
  - Pre-generation validation of all labware `load_name` values against the Opentrons labware library
  - Tighter schema constraints on command parameter combinations
  - Post-generation static analysis before simulation

- [ ] **Better RAG datasets** — The current 238 examples were auto-curated from public Opentrons protocols. Many are incomplete or lack module-heavy workflows. Improvements planned:
  - Curate a smaller, high-quality "gold standard" example set
  - Add more module-centric examples (thermocycler profiles, magnetic bead workflows)
  - Include failure cases so the model learns what *not* to do

- [ ] **User data-ingesting RAG** — Allow users to feed their own validated protocols into the RAG store so the system learns from lab-specific patterns and preferences. This would enable:
  - Per-lab customization without retraining
  - Incremental improvement as more protocols are validated
  - Shared protocol libraries within a research group

- [ ] **Comprehensive test suite** — Unit tests for Layer 1 (mock LLM responses), integration tests for the full pipeline, and CI/CD.

- [ ] **Multi-robot support** — Currently assumes a single OT-2. Extend `robot.py` to manage multiple robots and queue protocols.

- [ ] **Opentrons Flex support** — Extend beyond OT-2 to support the newer Opentrons Flex platform and its API differences.

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
