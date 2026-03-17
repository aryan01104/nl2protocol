"""
nl2protocol - Convert natural language to Opentrons robot protocols.

Usage:
    nl2protocol -i "Transfer 100uL from A1 to B1" -c lab_config.json

Or programmatically:
    from nl2protocol import ProtocolAgent

    agent = ProtocolAgent(config_path="lab_config.json")
    result = agent.run_pipeline(prompt="Your intent here")
"""

__version__ = "0.1.0"

from nl2protocol.app import ProtocolAgent, generate_opentrons_script, verify_protocol
from nl2protocol.parser import ProtocolParser
from nl2protocol.models import ProtocolSchema, Command, Labware, Pipette
from nl2protocol.validation import validate_config, validate_config_file, ConfigValidator
from nl2protocol.robot import RobotClient

__all__ = [
    # Version
    "__version__",
    # Main classes
    "ProtocolAgent",
    "ProtocolParser",
    "ProtocolSchema",
    "ConfigValidator",
    "RobotClient",
    # Functions
    "generate_opentrons_script",
    "verify_protocol",
    "validate_config",
    "validate_config_file",
    # Types
    "Command",
    "Labware",
    "Pipette",
]
