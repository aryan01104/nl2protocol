# nl2protocol: How It Works

## The Pipeline

```
Natural Language ─┬─> ProtocolSchema ───> Python Script ───> Opentrons Simulator
       +          │       (JSON)           (runnable)           (validates)
   CSV Data       │          ▲                                       │
       +          │          │ errors fed back                       │
  Lab Config      │          └───────────────────────────────────────┘
                  │                    (retry loop)
                  ▼
             Gemini LLM
```

## Files by Pipeline Stage

### Layer 1: Intent Parsing (NL → Schema)

| File | What It Does |
|------|--------------|
| `nl2protocol/parser.py` | The "reasoning engine". Sends prompt + config to Gemini LLM, parses response into `ProtocolSchema`. Contains few-shot examples and system prompts. |
| `nl2protocol/models.py` | Pydantic models that define the `ProtocolSchema` structure. All commands (transfer, aspirate, mix, etc.) are defined here with validation. |
| `nl2protocol/validation.py` | Validates `lab_config.json` before sending to LLM. Checks labware names exist, no slot conflicts, tipracks reference valid labware. |

### Layer 2: Code Generation (Schema → Script)

| File | What It Does |
|------|--------------|
| `nl2protocol/app.py` | `generate_opentrons_script()` - Converts `ProtocolSchema` to executable Python. Deterministic: same input = same output. |

### Layer 3: Verification (Script → Simulation)

| File | What It Does |
|------|--------------|
| `nl2protocol/app.py` | `verify_protocol()` - Runs script through Opentrons simulator. Returns success/failure + logs. |

### Orchestration

| File | What It Does |
|------|--------------|
| `nl2protocol/app.py` | `ProtocolAgent.run_pipeline()` - Ties it all together. Runs the retry loop (parse → generate → verify → feed errors back). |
| `nl2protocol/cli.py` | Command-line interface. Handles args, calls `ProtocolAgent`, saves output files. |
| `nl2protocol/robot.py` | HTTP client for OT-2 robot. Upload protocol, start runs, check status. |

## Data Flow Example

```
User: "Transfer 50uL from reservoir A1 to plate B1"
                    │
                    ▼
┌─────────────────────────────────────────────────────────┐
│ parser.py: ProtocolParser.parse_intent()                │
│   - Loads lab_config.json                               │
│   - Enriches config with well info                      │
│   - Sends to Gemini with schema + examples              │
│   - Parses JSON response into ProtocolSchema            │
└─────────────────────────────────────────────────────────┘
                    │
                    ▼ ProtocolSchema
┌─────────────────────────────────────────────────────────┐
│ app.py: generate_opentrons_script()                     │
│   - Loops through labware → load_labware() calls        │
│   - Loops through pipettes → load_instrument() calls    │
│   - Loops through commands → pip.transfer(), etc.       │
└─────────────────────────────────────────────────────────┘
                    │
                    ▼ Python script string
┌─────────────────────────────────────────────────────────┐
│ app.py: verify_protocol()                               │
│   - Wraps script in StringIO                            │
│   - Calls opentrons.simulate.simulate()                 │
│   - Returns (success, log) or (failure, error)          │
└─────────────────────────────────────────────────────────┘
                    │
            ┌───────┴───────┐
            │               │
         SUCCESS         FAILURE
            │               │
            ▼               ▼
       Save .py        Feed error back
       + log           to parser (retry)
```

## Testing

Currently only **Layer 2** (code generation) is tested.

```
tests/
├── test_codegen.py              # Snapshot/golden file tests
└── fixtures/codegen/
    ├── cases.json               # Test inputs (config + schema)
    └── golden/*.py              # Expected outputs
```

**How tests work:**
1. Load `ProtocolSchema` from `cases.json`
2. Run `generate_opentrons_script()`
3. Verify output passes Opentrons simulator (source of truth)
4. Compare to golden file (regression detection)

**Not yet tested:**
- Layer 1 (LLM parsing) - hard to test deterministically
- Layer 3 (verification) - implicitly tested via codegen tests
- End-to-end (NL → robot)

## Key Files Quick Reference

```
nl2protocol/
├── __init__.py          # Package init, version
├── __main__.py          # python -m nl2protocol entry point
├── app.py               # Core: generate script, verify, ProtocolAgent
├── cli.py               # Command-line interface
├── models.py            # Pydantic schemas (ProtocolSchema, Command types)
├── parser.py            # LLM integration (Gemini), prompt engineering
├── robot.py             # OT-2 HTTP API client
└── validation.py        # Config file validation
```
