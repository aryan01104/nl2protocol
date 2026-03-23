#!/bin/bash
# Run all test cases with and without config
# Results saved to test_cases/{name}/results_with_config/ and results_without_config/

set -e

cd "$(dirname "$0")/.."

TEST_CASES=(
    "simple_transfer"
    "distribute"
    "serial_dilution"
    "pcr_mastermix"
    "elisa_sample_addition"
    "magnetic_bead_cleanup"
    "qpcr_standard_curve"
    "cell_viability_assay"
)

echo "========================================"
echo "Running all test cases"
echo "========================================"

for test_name in "${TEST_CASES[@]}"; do
    test_dir="test_cases/${test_name}"
    instruction_file="${test_dir}/instruction.txt"
    config_file="${test_dir}/config.json"

    if [[ ! -f "$instruction_file" ]]; then
        echo "SKIP: ${test_name} - no instruction.txt"
        continue
    fi

    echo ""
    echo "========================================"
    echo "TEST: ${test_name}"
    echo "========================================"

    # Create output directories
    mkdir -p "${test_dir}/results_with_config"
    mkdir -p "${test_dir}/results_without_config"

    # Run WITH config
    echo ""
    echo "--- Running WITH config ---"
    if [[ -f "$config_file" ]]; then
        python -m nl2protocol \
            -i "$instruction_file" \
            -c "$config_file" \
            -o "${test_dir}/results_with_config/protocol.py" \
            -r 2 \
            2>&1 | tee "${test_dir}/results_with_config/run.log"

        if [[ $? -eq 0 ]]; then
            echo "SUCCESS: ${test_name} with config"
        else
            echo "FAILED: ${test_name} with config"
        fi
    else
        echo "SKIP: No config.json for ${test_name}"
    fi

    # Run WITHOUT config (--generate-config)
    echo ""
    echo "--- Running WITHOUT config (--generate-config) ---"
    python -m nl2protocol \
        -i "$instruction_file" \
        --generate-config \
        -o "${test_dir}/results_without_config/protocol.py" \
        -r 2 \
        2>&1 | tee "${test_dir}/results_without_config/run.log" << EOF
y
EOF

    if [[ $? -eq 0 ]]; then
        echo "SUCCESS: ${test_name} without config"
    else
        echo "FAILED: ${test_name} without config"
    fi
done

echo ""
echo "========================================"
echo "All tests complete!"
echo "========================================"
echo ""
echo "Results saved in test_cases/{name}/results_with_config/ and results_without_config/"
