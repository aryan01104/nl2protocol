# Data Curation Pipeline

Generate training data for the nl2protocol RAG system by reverse-engineering existing Opentrons protocol scripts.

## Overview

```
Existing Protocol Scripts (Opentrons GitHub, etc.)
         ↓
    script_parser.py  →  JSON Schema (labware, pipettes, commands)
         ↓
   intent_generator.py → 3 NL Intent Variations (technical, casual, concise)
         ↓
    (intent, schema) pairs saved to examples/
```

Each script produces 3 examples with different phrasing styles.

## Usage

### From local directory
```bash
python run_pipeline.py --input ./my_protocols/ --output ../examples/ --verbose
```

### From Opentrons GitHub
```bash
python run_pipeline.py --github Opentrons/Protocols --output ../examples/ --limit 50
```

### Process single script
```bash
# Just parse
python script_parser.py protocol.py

# Just generate intents
python intent_generator.py protocol.py
```

## Output Format

Each example is saved as a JSON file:
```json
{
    "id": "ex_technical_a1b2c3d4",
    "intent": "Transfer 100µL from wells A1-A8...",
    "style": "technical",
    "source_file": "serial_dilution.py",
    "output_schema": {
        "protocol_name": "Serial Dilution",
        "labware": [...],
        "pipettes": [...],
        "commands": [...]
    }
}
```

## Requirements

```bash
pip install anthropic python-dotenv
```

Set `ANTHROPIC_API_KEY` in your environment or `.env` file.

## Files

- `script_parser.py` - AST-based parser to extract schema from Python scripts
- `intent_generator.py` - LLM-based generator for NL descriptions (3 styles)
- `run_pipeline.py` - Main orchestration script
