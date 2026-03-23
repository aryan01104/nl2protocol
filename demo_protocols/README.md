# Demo Protocols - 5 Most Common Biology Lab Protocols

These are the 5 most commonly used protocols in biology/molecular biology labs, ready to demo with nl2protocol.

## Quick Reference

| Protocol | Description | Special Flags |
|----------|-------------|---------------|
| Bradford Assay | Protein quantification | Standard |
| Bacterial Transformation | Heat shock transformation | `--robot` |
| Plasmid Miniprep | Magnetic bead DNA extraction | Standard |
| Cell Seeding | Seed cells for experiments | `--generate-config` |
| Western Blot Prep | SDS-PAGE sample preparation | Standard |

---

## 1. Bradford Protein Assay

Standard protein quantification using Coomassie dye with BSA standard curve.

```bash
nl2protocol \
  -i demo_protocols/bradford_assay/instruction.txt \
  -c demo_protocols/bradford_assay/config.json \
  -o bradford_protocol.py
```

---

## 2. Bacterial Transformation (with Robot Upload)

Heat shock transformation of competent E. coli with plasmid DNA. Uses temperature module for heat shock control.

**This demo includes the `--robot` flag to upload directly to OT-2 after simulation.**

```bash
nl2protocol \
  -i demo_protocols/bacterial_transformation/instruction.txt \
  -c demo_protocols/bacterial_transformation/config.json \
  -o transformation_protocol.py \
  --robot
```

---

## 3. Plasmid Miniprep

Magnetic bead-based plasmid DNA extraction from bacterial cultures. Uses magnetic module.

```bash
nl2protocol \
  -i demo_protocols/plasmid_miniprep/instruction.txt \
  -c demo_protocols/plasmid_miniprep/config.json \
  -o miniprep_protocol.py
```

---

## 4. Cell Seeding (Auto-Generated Config)

Seed cells into 96-well plate for experiments.

**This demo uses `--generate-config` to automatically infer the lab configuration from the instruction (no config file needed).**

```bash
nl2protocol \
  -i demo_protocols/cell_seeding/instruction.txt \
  --generate-config \
  -o cell_seeding_protocol.py
```

---

## 5. Western Blot Sample Preparation

Prepare protein lysates with Laemmli buffer for SDS-PAGE. Uses temperature module for sample denaturation.

```bash
nl2protocol \
  -i demo_protocols/western_blot_prep/instruction.txt \
  -c demo_protocols/western_blot_prep/config.json \
  -o western_prep_protocol.py
```

---

## Run All Demos

Run all 5 demos sequentially (without robot upload):

```bash
# 1. Bradford Assay
nl2protocol -i demo_protocols/bradford_assay/instruction.txt -c demo_protocols/bradford_assay/config.json -o bradford_protocol.py

# 2. Bacterial Transformation
nl2protocol -i demo_protocols/bacterial_transformation/instruction.txt -c demo_protocols/bacterial_transformation/config.json -o transformation_protocol.py

# 3. Plasmid Miniprep
nl2protocol -i demo_protocols/plasmid_miniprep/instruction.txt -c demo_protocols/plasmid_miniprep/config.json -o miniprep_protocol.py

# 4. Cell Seeding (auto-config)
nl2protocol -i demo_protocols/cell_seeding/instruction.txt --generate-config -o cell_seeding_protocol.py

# 5. Western Blot Prep
nl2protocol -i demo_protocols/western_blot_prep/instruction.txt -c demo_protocols/western_blot_prep/config.json -o western_prep_protocol.py
```

---

## Robot Configuration

The project includes a `robot_config.json` at the project root with **demo mode enabled by default**:

```json
{
    "robot_ip": "192.168.1.100",
    "robot_name": "Demo OT-2",
    "demo_mode": true
}
```

### Demo Mode (Current Setup)

With `demo_mode: true`, the `--robot` flag will:
- Simulate successful connection to robot
- Simulate protocol upload
- Simulate run creation and start
- Print `[DEMO MODE]` messages to show what would happen

This allows you to demo the full workflow without an actual robot connected.

### Connecting to a Real Robot

To use with an actual OT-2 robot:

1. Edit `robot_config.json` in the project root:
   ```json
   {
       "robot_ip": "YOUR_ROBOT_IP",
       "robot_name": "My Lab OT-2",
       "demo_mode": false
   }
   ```

2. Find your robot's IP address:
   - On the OT-2 touchscreen: Settings > Network > WiFi/Ethernet
   - Or use the Opentrons App to discover robots on your network

3. Ensure your computer and robot are on the same network

4. Run the protocol with `--robot` flag - it will now actually upload and run

---

## Notes

- Ensure `ANTHROPIC_API_KEY` is set in your environment or `.env` file
- Run `nl2protocol --setup` if you need to configure your API key
- Generated protocols are timestamped automatically (e.g., `bradford_protocol_20240315_143022.py`)
- Simulation logs are saved alongside protocols (e.g., `bradford_protocol_20240315_143022_simulation.log`)
