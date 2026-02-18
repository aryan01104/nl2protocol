"""
Test Layer 1: Models + Validation (no external dependencies)
Run with: python test_models.py
"""

from models import ProtocolSchema, Labware, Pipette, Transfer, Mix, Distribute

# =============================================================================
# TEST DATA
# =============================================================================

# Mock config (simulates lab_config.json)
MOCK_CONFIG = {
    "labware": {
        "reservoir": {
            "load_name": "nest_12_reservoir_15ml",
            "slot": "1",
            "label": "Reservoir"
        },
        "plate": {
            "load_name": "corning_96_wellplate_360ul_flat",
            "slot": "2",
            "label": "Plate"
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

# Valid protocol data (what LLM might output)
VALID_PROTOCOL = {
    "protocol_name": "Test Serial Dilution",
    "author": "Test",
    "labware": [
        {"slot": "1", "load_name": "nest_12_reservoir_15ml", "label": "Reservoir"},
        {"slot": "2", "load_name": "corning_96_wellplate_360ul_flat", "label": "Plate"},
        {"slot": "3", "load_name": "opentrons_96_tiprack_300ul", "label": "Tips"}
    ],
    "pipettes": [
        {"mount": "left", "model": "p300_single_gen2", "tipracks": ["Tips"]}
    ],
    "commands": [
        {"command_type": "transfer", "pipette": "left", "source_labware": "Reservoir",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 100},
        {"command_type": "mix", "pipette": "left", "labware": "Plate",
         "well": "A1", "volume": 50, "repetitions": 3}
    ]
}


# =============================================================================
# TEST FUNCTIONS
# =============================================================================

def test_valid_protocol():
    """Test that a valid protocol passes validation."""
    print("\n=== Test: Valid Protocol ===")
    try:
        protocol = ProtocolSchema.model_validate(
            VALID_PROTOCOL,
            context={'config': MOCK_CONFIG}
        )
        print(f"✓ Protocol created: {protocol.protocol_name}")
        print(f"✓ Labware count: {len(protocol.labware)}")
        print(f"✓ Command count: {len(protocol.commands)}")
        return True
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


def test_invalid_labware_reference():
    """Test that referencing non-existent labware fails."""
    print("\n=== Test: Invalid Labware Reference ===")
    bad_protocol = VALID_PROTOCOL.copy()
    bad_protocol["commands"] = [
        {"command_type": "transfer", "pipette": "left", "source_labware": "FakeLabware",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 100}
    ]

    try:
        ProtocolSchema.model_validate(bad_protocol, context={'config': MOCK_CONFIG})
        print("✗ Should have failed but didn't!")
        return False
    except ValueError as e:
        if "FakeLabware" in str(e):
            print(f"✓ Correctly rejected: {e}")
            return True
        else:
            print(f"✗ Wrong error: {e}")
            return False


def test_invalid_pipette_reference():
    """Test that referencing non-existent pipette fails."""
    print("\n=== Test: Invalid Pipette Reference ===")
    bad_protocol = VALID_PROTOCOL.copy()
    bad_protocol["commands"] = [
        {"command_type": "transfer", "pipette": "right", "source_labware": "Reservoir",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 100}
    ]

    try:
        ProtocolSchema.model_validate(bad_protocol, context={'config': MOCK_CONFIG})
        print("✗ Should have failed but didn't!")
        return False
    except ValueError as e:
        if "right" in str(e) and "pipette" in str(e).lower():
            print(f"✓ Correctly rejected: {e}")
            return True
        else:
            print(f"✗ Wrong error: {e}")
            return False


def test_volume_out_of_range():
    """Test that volume exceeding pipette capacity fails."""
    print("\n=== Test: Volume Out of Range ===")
    bad_protocol = VALID_PROTOCOL.copy()
    bad_protocol["commands"] = [
        {"command_type": "transfer", "pipette": "left", "source_labware": "Reservoir",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 500}
    ]

    try:
        ProtocolSchema.model_validate(bad_protocol, context={'config': MOCK_CONFIG})
        print("✗ Should have failed but didn't!")
        return False
    except ValueError as e:
        if "500" in str(e) and "range" in str(e).lower():
            print(f"✓ Correctly rejected: {e}")
            return True
        else:
            print(f"✗ Wrong error: {e}")
            return False


def test_config_mismatch_labware():
    """Test that labware not in config fails."""
    print("\n=== Test: Labware Not in Config ===")
    bad_protocol = VALID_PROTOCOL.copy()
    bad_protocol["labware"] = [
        {"slot": "1", "load_name": "fake_labware_that_doesnt_exist", "label": "Fake"},
        {"slot": "2", "load_name": "corning_96_wellplate_360ul_flat", "label": "Plate"},
        {"slot": "3", "load_name": "opentrons_96_tiprack_300ul", "label": "Tips"}
    ]
    bad_protocol["commands"] = [
        {"command_type": "transfer", "pipette": "left", "source_labware": "Fake",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 100}
    ]

    try:
        ProtocolSchema.model_validate(bad_protocol, context={'config': MOCK_CONFIG})
        print("✗ Should have failed but didn't!")
        return False
    except ValueError as e:
        if "config" in str(e).lower() or "fake_labware" in str(e).lower():
            print(f"✓ Correctly rejected: {e}")
            return True
        else:
            print(f"✗ Wrong error: {e}")
            return False


def test_config_mismatch_pipette():
    """Test that pipette model not matching config fails."""
    print("\n=== Test: Pipette Model Mismatch ===")
    bad_protocol = VALID_PROTOCOL.copy()
    bad_protocol["pipettes"] = [
        {"mount": "left", "model": "p1000_single_gen2", "tipracks": ["Tips"]}
    ]

    try:
        ProtocolSchema.model_validate(bad_protocol, context={'config': MOCK_CONFIG})
        print("✗ Should have failed but didn't!")
        return False
    except ValueError as e:
        if "p1000" in str(e).lower() or "mismatch" in str(e).lower():
            print(f"✓ Correctly rejected: {e}")
            return True
        else:
            print(f"✗ Wrong error: {e}")
            return False


def test_mix_after_tuple():
    """Test that mix_after as array gets converted properly."""
    print("\n=== Test: mix_after Tuple Handling ===")
    protocol_with_mix = VALID_PROTOCOL.copy()
    protocol_with_mix["commands"] = [
        {"command_type": "transfer", "pipette": "left", "source_labware": "Reservoir",
         "source_well": "A1", "dest_labware": "Plate", "dest_well": "A1", "volume": 100,
         "mix_after": [3, 50]}  # JSON array, should become tuple
    ]

    try:
        protocol = ProtocolSchema.model_validate(
            protocol_with_mix,
            context={'config': MOCK_CONFIG}
        )
        cmd = protocol.commands[0]
        if hasattr(cmd, 'mix_after') and cmd.mix_after is not None:
            print(f"✓ mix_after value: {cmd.mix_after}")
            print(f"✓ mix_after type: {type(cmd.mix_after)}")
            return True
        else:
            print("✗ mix_after not found")
            return False
    except Exception as e:
        print(f"✗ Failed: {e}")
        return False


# =============================================================================
# RUN TESTS
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("LAYER 1: MODEL + VALIDATION TESTS")
    print("=" * 60)

    results = []
    results.append(("Valid Protocol", test_valid_protocol()))
    results.append(("Invalid Labware Ref", test_invalid_labware_reference()))
    results.append(("Invalid Pipette Ref", test_invalid_pipette_reference()))
    results.append(("Volume Out of Range", test_volume_out_of_range()))
    results.append(("Config Mismatch Labware", test_config_mismatch_labware()))
    results.append(("Config Mismatch Pipette", test_config_mismatch_pipette()))
    results.append(("mix_after Tuple", test_mix_after_tuple()))

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = sum(1 for _, r in results if r)
    total = len(results)

    for name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"  {status}: {name}")

    print(f"\nTotal: {passed}/{total} passed")
