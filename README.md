# BioLab

An AI-powered system that translates natural language instructions into executable Opentrons robot protocols.

BioLab bridges the gap between scientific intent and robotic action. Describe your lab procedure in plain English, and BioLab generates validated, simulator-tested Python code ready to run on Opentrons liquid handling robots.

## How It Works

BioLab uses a three-layer architecture with a self-correction loop:

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: REASONING                                         │
│  Natural language + CSV data → ProtocolSchema               │
│  (Google Gemini 2.0 Flash)                                  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2: GENERATION                                        │
│  ProtocolSchema → Executable Python script                  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3: VERIFICATION                                      │
│  Opentrons Simulator validates safety before execution      │
└─────────────────────────────────────────────────────────────┘
                              ↓
                    ┌────────────────┐
                    │ Pass? → Output │
                    │ Fail? → Retry  │←─────┐
                    └────────────────┘      │
                              │             │
                              └─────────────┘
                           (up to 3 attempts)
```

If verification fails, the error is fed back to the reasoning layer for automatic correction.

## Features

- **Natural Language Input**: Describe experiments in plain English
- **CSV Data Integration**: Import experimental parameters from spreadsheets
- **Strict Validation**: Pydantic schemas enforce protocol correctness
- **Config-Aware**: Validates against your actual lab hardware setup
- **Simulator-Tested**: Every protocol passes Opentrons simulation before output
- **Self-Correcting**: Automatically fixes errors through LLM feedback loop

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/biolab.git
cd biolab

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up API key
echo "GEMINI_API_KEY=your_api_key_here" > .env
```

## Quick Start

1. **Define your lab configuration** in `lab_config.json`:

```json
{
  "labware": {
    "source_plate": {
      "load_name": "corning_96_wellplate_360ul_flat",
      "slot": "1",
      "label": "Source Plate"
    },
    "dest_plate": {
      "load_name": "nest_96_wellplate_200ul_flat",
      "slot": "2",
      "label": "Dest Plate"
    },
    "tips": {
      "load_name": "opentrons_96_tiprack_300ul",
      "slot": "3",
      "label": "Tips"
    }
  },
  "pipettes": {
    "left": {
      "model": "p300_single_gen2",
      "tipracks": ["Tips"]
    }
  }
}
```

2. **Run the agent** with your intent:

```python
from app import ProtocolAgent

agent = ProtocolAgent()
script = agent.run_pipeline(
    prompt="Transfer 50uL from wells A1-A8 of the source plate to the same wells in the dest plate",
    csv_path="experiment_data.csv"  # optional
)

if script:
    with open("my_protocol.py", "w") as f:
        f.write(script)
```

3. **Run on your robot** or test in the Opentrons simulator.

## Supported Commands

### Atomic Commands
| Command | Description |
|---------|-------------|
| `aspirate` | Draw liquid into pipette tip |
| `dispense` | Push liquid out of pipette tip |
| `mix` | Aspirate/dispense cycles to mix |
| `blow_out` | Expel remaining liquid/air |
| `touch_tip` | Touch tip to well sides |
| `air_gap` | Add air gap to prevent dripping |
| `pick_up_tip` | Pick up a pipette tip |
| `drop_tip` | Drop current tip |
| `return_tip` | Return tip to original location |

### Complex Commands
| Command | Description |
|---------|-------------|
| `transfer` | Move liquid from source to destination |
| `distribute` | One source to multiple destinations |
| `consolidate` | Multiple sources to one destination |

## Validation Layers

BioLab enforces correctness at multiple levels:

1. **Config Validation** (Field Validators)
   - Labware must exist in lab config (load_name, slot, label)
   - Pipette models must match config exactly
   - Tipracks must match config

2. **Reference Validation** (Model Validator)
   - Commands must reference defined labware
   - Commands must reference defined pipettes
   - Volumes must be within pipette capacity (e.g., p300: 20-300µL)

3. **Simulation Validation** (Opentrons Simulator)
   - Full protocol execution in virtual environment
   - Catches runtime errors before physical execution

## Project Structure

```
biolab/
├── models.py          # Pydantic schemas for protocol validation
├── parser.py          # LLM reasoning engine (Gemini integration)
├── app.py             # Protocol generation and verification
├── lab_config.json    # Your lab hardware configuration
├── test_models.py     # Validation test suite
├── configs/           # Example lab configurations
│   ├── serial_dilution_config.json
│   ├── plate_replication_config.json
│   └── reagent_distribution_config.json
└── data/              # Example CSV experiment data
    ├── serial_dilution.csv
    ├── plate_replication.csv
    └── reagent_distribution.csv
```

## Testing

```bash
python test_models.py
```

Tests cover:
- Valid protocol creation
- Invalid labware/pipette references
- Volume range validation
- Config mismatch detection
- Tuple handling for mix parameters

## Example Use Cases

**Serial Dilution**
```
"Perform a 2x serial dilution across row A. Stock is in reservoir A2,
diluent in A1. Pre-fill destination wells with 100uL diluent."
```

**Plate Replication**
```
"Copy 50uL from each well in column 1 of the source plate to the
same wells in the destination plate."
```

**Reagent Distribution**
```
"Add 100uL of buffer from reservoir A1 to all wells in row A of
the assay plate."
```

## Requirements

- Python 3.10+
- Pydantic v2
- Google Generative AI SDK
- Opentrons API
- python-dotenv

## License

MIT
