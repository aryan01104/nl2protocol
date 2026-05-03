"""
errors.py

Custom exceptions with user-friendly error messages for nl2protocol.
"""


class NL2ProtocolError(Exception):
    """Base exception for nl2protocol errors."""
    pass


class ConfigurationError(NL2ProtocolError):
    """Error related to configuration files or settings."""
    pass


class APIKeyError(ConfigurationError):
    """Missing or invalid API key."""

    def __init__(self, key_name: str = "ANTHROPIC_API_KEY"):
        self.key_name = key_name
        message = f"""
API key not found: {key_name}

To fix this:
1. Get an API key from https://console.anthropic.com/
2. Set the environment variable:
   export {key_name}="your-api-key-here"

   Or create a .env file in your project root:
   {key_name}=your-api-key-here
"""
        super().__init__(message)


class ConfigFileError(ConfigurationError):
    """Error loading or parsing config file."""

    def __init__(self, config_path: str, reason: str = ""):
        self.config_path = config_path
        message = f"""
Configuration error: {config_path}
{reason}

Expected format (lab_config.json):
{{
    "labware": {{
        "tiprack": {{"load_name": "opentrons_96_tiprack_300ul", "slot": "1"}},
        "plate": {{"load_name": "corning_96_wellplate_360ul_flat", "slot": "2"}}
    }},
    "pipettes": {{
        "left": {{"model": "p300_single_gen2", "tipracks": ["tiprack"]}}
    }}
}}

See lab_config.example.json for a complete example.
"""
        super().__init__(message)


class ValidationError(NL2ProtocolError):
    """Error validating user input or generated content."""
    pass


class InputValidationError(ValidationError):
    """User input is not a valid protocol instruction."""

    def __init__(self, classification: str, reason: str, suggestion: str = None):
        self.classification = classification
        self.reason = reason
        self.suggestion = suggestion

        message = f"""
Invalid input: {reason}

Your input was classified as: {classification}
"""
        if suggestion:
            message += f"\nSuggestion: {suggestion}"

        message += """

Examples of valid protocol instructions:
- "Transfer 100uL from well A1 to B1"
- "Perform a serial dilution across row A"
- "Distribute 50uL of reagent to all wells in column 1"
"""
        super().__init__(message)


class EquipmentError(NL2ProtocolError):
    """Error related to lab equipment configuration."""
    pass


class LabwareNotFoundError(EquipmentError):
    """Referenced labware not found in config."""

    def __init__(self, labware_name: str, available: list = None):
        self.labware_name = labware_name
        message = f"Labware '{labware_name}' not found in configuration."
        if available:
            message += f"\n\nAvailable labware: {', '.join(available)}"
        message += "\n\nCheck your lab_config.json or use --generate-config to auto-detect equipment."
        super().__init__(message)


class PipetteNotFoundError(EquipmentError):
    """Referenced pipette not found in config."""

    def __init__(self, mount: str, available: list = None):
        self.mount = mount
        message = f"No pipette configured for mount '{mount}'."
        if available:
            message += f"\n\nAvailable mounts: {', '.join(available)}"
        super().__init__(message)


class ModuleNotFoundError(EquipmentError):
    """Referenced module not found in config."""

    def __init__(self, module_name: str, available: list = None):
        self.module_name = module_name
        message = f"Module '{module_name}' not found in configuration."
        if available:
            message += f"\n\nAvailable modules: {', '.join(available)}"
        super().__init__(message)


class GenerationError(NL2ProtocolError):
    """Error during protocol generation."""
    pass


class SimulationError(GenerationError):
    """Protocol simulation failed."""

    def __init__(self, error_message: str):
        message = f"""
Protocol simulation failed: {error_message}

This usually means the generated protocol has an error.
The system will attempt to self-correct and retry.
"""
        super().__init__(message)


class RobotConnectionError(NL2ProtocolError):
    """Error connecting to OT-2 robot."""

    def __init__(self, robot_ip: str, reason: str = ""):
        self.robot_ip = robot_ip
        message = f"""
Could not connect to robot at {robot_ip}
{reason}

Troubleshooting:
1. Ensure the robot is powered on and connected to the network
2. Verify the IP address is correct
3. Check that your computer is on the same network as the robot
4. Try pinging the robot: ping {robot_ip}
"""
        super().__init__(message)


def format_error_for_cli(error: Exception) -> str:
    """Format an exception for CLI display, distinguishing project errors from others.

    Pre:    `error` is any Exception instance (including any subclass).

    Post:   If `error` is an instance of `NL2ProtocolError` (or any subclass —
            ConfigurationError, ValidationError, EquipmentError,
            GenerationError, etc.): returns `f"Error: {str(error)}"`.
            Otherwise: returns
            `f"Unexpected error: {type(error).__name__}: {str(error)}"`.
            The exact-class name (not the parent class name) appears for
            non-project errors.

    Side effects: None. Pure, deterministic.

    Raises: Never (does not propagate exceptions from `str(error)`).
    """
    if isinstance(error, NL2ProtocolError):
        return f"Error: {str(error)}"
    else:
        return f"Unexpected error: {type(error).__name__}: {str(error)}"


def format_api_error(e: Exception) -> str:
    """Convert an Anthropic API exception into a one-line actionable message.

    Pre:    `e` is any Exception (typically one raised by the Anthropic SDK,
            but the function handles arbitrary exceptions via the catch-all
            else branch).

    Post:   Dispatch table by `isinstance` (checked in this order; first match
            wins):
              * `anthropic.AuthenticationError` → "API authentication failed.
                Check your ANTHROPIC_API_KEY is valid and not expired."
              * `anthropic.RateLimitError` → "Rate limited by the API.
                Wait 30-60 seconds and try again."
              * `anthropic.APIStatusError` → message depends on str(e).lower():
                  - contains "credit" OR "balance" → credit-balance message
                  - contains "overloaded" → overloaded message
                  - otherwise → "API error (status {e.status_code}). ..."
              * `anthropic.APITimeoutError` → timeout message
              * `anthropic.APIConnectionError` → connection message
              * Anything else → "Unexpected error: {type(e).__name__}"
            All returned messages are one line and end with an actionable
            instruction the scientist can follow.

    Side effects: Lazy imports `anthropic` on first call. No I/O, no mutation.

    Raises: Never (the catch-all else branch handles any non-anthropic
            exception type).
    """
    import anthropic

    if isinstance(e, anthropic.AuthenticationError):
        return "API authentication failed. Check your ANTHROPIC_API_KEY is valid and not expired."
    elif isinstance(e, anthropic.RateLimitError):
        return "Rate limited by the API. Wait 30-60 seconds and try again."
    elif isinstance(e, anthropic.APIStatusError):
        msg = str(e).lower()
        if "credit" in msg or "balance" in msg:
            return "Anthropic API credit balance is too low. Add credits at console.anthropic.com"
        elif "overloaded" in msg:
            return "The API is temporarily overloaded. Try again in a few minutes."
        return f"API error (status {e.status_code}). This is usually transient — retry in a moment."
    elif isinstance(e, anthropic.APITimeoutError):
        return "API request timed out. Check your network connection and try again."
    elif isinstance(e, anthropic.APIConnectionError):
        return "Could not connect to the Anthropic API. Check your internet connection."
    else:
        return f"Unexpected error: {type(e).__name__}"
