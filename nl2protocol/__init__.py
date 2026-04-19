"""
nl2protocol - Convert natural language to Opentrons robot protocols using Claude LLM.

Usage:
    nl2protocol -i "Transfer 100uL from A1 to B1" -c lab_config.json
    nl2protocol -i "Serial dilution across row A" --generate-config

Or programmatically:
    from nl2protocol import ProtocolAgent

    agent = ProtocolAgent(config_path="lab_config.json")
    result = agent.run_pipeline(prompt="Your intent here")
    print(result.script)
"""

__version__ = "0.2.0"

from nl2protocol.app import ProtocolAgent, generate_python_script, simulate_script
from nl2protocol.parser import ProtocolParser
from nl2protocol.models import ProtocolSchema, Command, Labware, Pipette, Pause, Delay, Comment
from nl2protocol.validate_config import validate_config, validate_config_file, ConfigValidator
from nl2protocol.robot import RobotClient
from nl2protocol.input_validator import InputValidator, validate_input
from nl2protocol.example_store import ExampleStore
from nl2protocol.extractor import SemanticExtractor, ProtocolSpec

__all__ = [
    # Version
    "__version__",
    # Main classes
    "ProtocolAgent",
    "ProtocolParser",
    "ProtocolSchema",
    "ConfigValidator",
    "RobotClient",
    "InputValidator",
    "ExampleStore",
    "SemanticExtractor",
    "ProtocolSpec",
    # Functions
    "generate_python_script",
    "simulate_script",
    "validate_config",
    "validate_config_file",
    "validate_input",
    # Types
    "Command",
    "Labware",
    "Pipette",
    "Pause",
    "Delay",
    "Comment",
]
