# nl2protocol

Convert natural language instructions into executable [Opentrons](https://opentrons.com/) OT-2 robot protocols using Claude LLM.

Describe your lab procedure in plain English, and nl2protocol generates validated, simulator-tested Python code ready to run on Opentrons OT-2 robots.

## How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  LAYER 1: REASONING                                         │
│  Natural language + Config → ProtocolSchema                 │
│  (Claude Sonnet with RAG examples)                          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 2: GENERATION                                        │
│  ProtocolSchema → Executable Python script                  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  LAYER 3: VERIFICATION                                      │
│  Opentrons Simulator + LLM Intent Verification              │
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

## Features

- **Natural Language Input**: Describe experiments in plain English
- **Auto-Config Generation**: Use `--generate-config` to infer equipment from your instruction
- **CSV Data Integration**: Import experimental parameters from spreadsheets
- **Hardware Module Support**: Temperature, magnetic, heater-shaker, and thermocycler modules
- **Simulator-Tested**: Every protocol passes Opentrons simulation before output
- **Self-Correcting**: Automatically fixes errors through LLM feedback loop
- **Robot Upload**: Push protocols directly to your OT-2 robot

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/nl2protocol.git
cd nl2protocol

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install package
pip install -e .

# Set up API key
export ANTHROPIC_API_KEY="your_api_key_here"
# Or create a .env file:
echo "ANTHROPIC_API_KEY=your_api_key_here" > .env
```

Get your API key from https://console.anthropic.com/

## Quick Start

### Option 1: Auto-generate config (easiest)

```bash
nl2protocol -i "Transfer 100uL from source plate A1 to dest plate B1" --generate-config
```

The system will infer what equipment you need and ask you to confirm.

### Option 2: Use existing config

1. Copy the example config:
```bash
cp lab_config.example.json lab_config.json
# Edit to match your actual equipment
```

2. Run:
```bash
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

## CLI Usage

```bash
# Basic usage
nl2protocol -i "Your protocol instruction" -c lab_config.json

# Auto-generate config
nl2protocol -i "Serial dilution across row A" --generate-config

# Read instruction from file
nl2protocol -i protocol.txt -c lab_config.json

# With CSV data
nl2protocol -i "Distribute samples per data file" -c lab.json -d samples.csv

# Upload to robot after generation
nl2protocol -i "Mix wells A1-A6" -c lab.json --robot

# Validate config file only
nl2protocol --validate-only -c lab_config.json

# Show help
nl2protocol --help
```

## Configuration

### Lab Config (lab_config.json)

```json
{
    "labware": {
        "tiprack": {
            "load_name": "opentrons_96_tiprack_300ul",
            "slot": "1"
        },
        "source_plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "2"
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
    }
}
```

See `lab_config.example.json` for a complete example with modules.

### Robot Config (robot_config.json)

```json
{
    "robot_ip": "192.168.1.100",
    "robot_name": "My OT-2"
}
```

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
| `pick_up_tip` | Pick up a pipette tip |
| `drop_tip` | Drop current tip |

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

## Example Instructions

**Serial Dilution**
```
Perform a 2x serial dilution across row A. Start with 200uL stock in A1.
```

**Plate Replication**
```
Copy 50uL from each well in column 1 of source plate to dest plate.
```

**Temperature-Controlled Transfer**
```
Set temperature module to 4C, wait for temperature, then transfer samples.
```

**PCR Setup**
```
Distribute 10uL master mix from tube rack to PCR plate wells A1-H12.
```

## Project Structure

```
nl2protocol/
├── nl2protocol/           # Main package
│   ├── app.py             # Protocol generation pipeline
│   ├── parser.py          # LLM reasoning (Claude)
│   ├── models.py          # Pydantic schemas
│   ├── cli.py             # Command-line interface
│   ├── robot.py           # OT-2 HTTP client
│   ├── config_generator.py # Auto-config from NL
│   ├── input_validator.py # Input classification
│   ├── example_store.py   # RAG with ChromaDB
│   └── errors.py          # Custom exceptions
├── examples/              # RAG training examples
├── data_curation/         # Tools to generate examples
├── lab_config.example.json
├── robot_config.example.json
└── pyproject.toml
```

## Requirements

- Python 3.10+
- Anthropic API key (Claude)
- Opentrons API

## License

MIT
