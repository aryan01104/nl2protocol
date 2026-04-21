#!/usr/bin/env python3
"""
Command-line interface for nl2protocol.

Convert natural language instructions to Opentrons OT-2 protocol scripts.

Usage:
    nl2protocol -i "Your protocol description" -c lab_config.json
    nl2protocol -i "Your protocol description" --generate-config
    nl2protocol -i protocol.txt -c my_lab.json
    nl2protocol -i experiment.pdf -d data.csv

Options:
    -i, --intent        Protocol description (text, .txt file, or .pdf file)
    -c, --config        Lab configuration JSON file (default: lab_config.json)
    -d, --data          CSV data file with experiment parameters
    -o, --output        Output protocol filename (timestamp auto-appended)
    -r, --retries       Max retry attempts (default: 3)
    --generate-config   Infer lab config from instruction (no config file needed)
    --robot             After success, prompt to upload to OT-2 robot
    --validate-only     Only validate config file, don't generate protocol

Notes:
    - Use --generate-config if you don't have a lab_config.json file
    - Output files are timestamped (e.g., protocol_20240315_143022.py)
    - Requires ANTHROPIC_API_KEY environment variable
"""
# Suppress verbose logging BEFORE any imports that use HuggingFace
import os
import sys
import warnings

os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# This disables the "unauthenticated requests" warning
os.environ["HF_HUB_VERBOSITY"] = "error"

# Suppress warnings
warnings.filterwarnings("ignore", message=".*unauthenticated.*")
warnings.filterwarnings("ignore", message=".*HF_TOKEN.*")
warnings.filterwarnings("ignore", category=UserWarning, module="huggingface_hub")

import logging
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        prog='nl2protocol',
        description='Convert natural language to Opentrons OT-2 protocols.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Basic usage with existing config
  %(prog)s -i "Transfer 100uL from plate A1 to B1" -c lab_config.json

  # Auto-generate config from instruction (no config file needed)
  %(prog)s -i "Serial dilution across row A" --generate-config

  # Read instruction from file
  %(prog)s -i protocol.txt -c my_lab.json

  # With CSV data for parameterized protocols
  %(prog)s -i "Distribute samples per data file" -c lab.json -d samples.csv

  # Upload to robot after generation
  %(prog)s -i "Mix wells A1-A6" -c lab.json --robot

  # Validate config file only
  %(prog)s --validate-only -c lab_config.json

Sample instructions:
  "Transfer 100uL from source plate well A1 to dest plate well B1"
  "Perform a 2x serial dilution across row A starting with 200uL"
  "Distribute 50uL from reservoir to all wells in column 1"
  "Mix 3 times at 100uL in wells A1 through A8"
  "Set temperature module to 37C, wait, then transfer samples"

Environment:
  ANTHROPIC_API_KEY    Required. Get from https://console.anthropic.com/

Files:
  lab_config.json      Lab equipment configuration (see lab_config.example.json)
  robot_config.json    Robot connection settings (see robot_config.example.json)
        '''
    )

    parser.add_argument(
        '-i', '--intent',
        default=None,
        help='Protocol description: literal text, .txt file, or .pdf file'
    )

    parser.add_argument(
        '-c', '--config',
        default='lab_config.json',
        help='Path to lab configuration JSON file (default: lab_config.json)'
    )

    parser.add_argument(
        '-d', '--data',
        default=None,
        help='Path to CSV data file with experiment parameters (optional)'
    )

    parser.add_argument(
        '-o', '--output',
        default='generated_protocol.py',
        help='Output base path - timestamp auto-appended (default: generated_protocol.py)'
    )

    parser.add_argument(
        '-r', '--retries',
        type=int,
        default=3,
        help='Maximum retry attempts for protocol generation (default: 3)'
    )

    parser.add_argument(
        '--robot',
        action='store_true',
        help='After successful simulation, prompt to upload protocol to robot'
    )

    parser.add_argument(
        '--validate-only',
        action='store_true',
        help='Only validate config file, do not generate protocol'
    )

    parser.add_argument(
        '--generate-config',
        action='store_true',
        help='[Deprecated] Infer lab config from instruction. Use -c with a config file instead.'
    )

    parser.add_argument(
        '-v', '--version',
        action='store_true',
        help='Show version and exit'
    )

    parser.add_argument(
        '--setup',
        action='store_true',
        help='Interactive setup to configure your Anthropic API key'
    )

    parser.add_argument(
        '--init',
        action='store_true',
        help='Copy starter lab config and .env to current directory'
    )

    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Validate CLI arguments and raise errors for invalid inputs."""
    # Config file only required if not using --generate-config
    if not args.generate_config and not os.path.isfile(args.config):
        raise FileNotFoundError(f"Config file not found: {args.config}")

    if args.data and not os.path.isfile(args.data):
        raise FileNotFoundError(f"Data file not found: {args.data}")

    if args.retries < 1:
        raise ValueError("--retries must be at least 1")

    # Ensure output directory exists
    output_dir = Path(args.output).parent
    if output_dir and str(output_dir) != '.' and not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)


def get_simulation_log_path(protocol_path: str) -> str:
    """Derive simulation log path from protocol output path."""
    path = Path(protocol_path)
    return str(path.parent / f"{path.stem}_simulation.log")


def get_timestamped_output_path(output_path: str) -> str:
    """Append timestamp to output filename."""
    path = Path(output_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return str(path.parent / f"{path.stem}_{timestamp}{path.suffix}")


def resolve_intent(intent: str) -> str:
    """Resolve intent - read from file if it's a .txt or .pdf path, else return as-is.

    Args:
        intent: Either literal text or a path to .txt/.pdf file

    Returns:
        The intent text (from file or as provided)

    Raises:
        FileNotFoundError: If file path provided but doesn't exist
        ValueError: If PDF parsing fails or pypdf not installed
    """
    path = Path(intent)
    suffix = path.suffix.lower()

    # If it looks like a file path and exists, read it
    if suffix in ('.txt', '.pdf') and path.exists():
        if suffix == '.txt':
            print(f"Reading intent from: {intent}")
            return path.read_text().strip()

        elif suffix == '.pdf':
            try:
                import pypdf
            except ImportError:
                raise ValueError(
                    "PDF support requires pypdf. Install with: pip install pypdf"
                )

            print(f"Reading intent from: {intent}")
            try:
                reader = pypdf.PdfReader(intent)
                text_parts = []
                for page in reader.pages:
                    text_parts.append(page.extract_text())
                return "\n".join(text_parts).strip()
            except Exception as e:
                raise ValueError(f"Failed to read PDF: {e}")

    # Otherwise treat as literal text
    return intent


def handle_init() -> int:
    """Copy starter config files to the current directory."""
    import shutil

    pkg_dir = Path(__file__).parent
    config_src = pkg_dir / "config_examples" / "lab_config.example.json"
    config_dst = Path.cwd() / "lab_config.json"
    env_dst = Path.cwd() / ".env"

    print("\nnl2protocol project setup")
    print("=" * 40)

    files_created = []

    # Copy lab config
    if config_dst.exists():
        print(f"\n  lab_config.json already exists, skipping.")
    else:
        shutil.copy(config_src, config_dst)
        print(f"\n  Created: lab_config.json")
        print(f"  Edit this to match your actual deck layout.")
        files_created.append("lab_config.json")

    # Create .env if missing
    if env_dst.exists():
        print(f"  .env already exists, skipping.")
    else:
        env_dst.write_text("ANTHROPIC_API_KEY=your-api-key-here\n")
        print(f"  Created: .env")
        print(f"  Replace 'your-api-key-here' with your key from console.anthropic.com")
        files_created.append(".env")

    if not files_created:
        print("\n  Nothing to do — config files already exist.")
    else:
        print(f"\nNext steps:")
        if ".env" in files_created:
            print(f"  1. Edit .env with your Anthropic API key")
        print(f"  {'2' if '.env' in files_created else '1'}. Edit lab_config.json to match your deck")
        print(f"  Then run:")
        print(f'    nl2protocol -i "Transfer 100uL from source_plate A1 to dest_plate B1" -c lab_config.json')

    return 0


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no input."""
    suffix = "[Y/n]" if default else "[y/N]"
    response = input(f"{question} {suffix}: ").strip().lower()
    if not response:
        return default
    return response in ('y', 'yes')


def handle_setup() -> int:
    """
    Interactive setup for API key configuration.

    Returns:
        0 on success, 1 on failure/cancel
    """
    from dotenv import load_dotenv
    load_dotenv()

    print("\n" + "=" * 50)
    print("nl2protocol Setup")
    print("=" * 50)

    # Check for existing key
    existing_key = os.getenv("ANTHROPIC_API_KEY")
    if existing_key:
        masked = existing_key[:8] + "..." + existing_key[-4:] if len(existing_key) > 12 else "****"
        print(f"\nExisting API key found: {masked}")
        if not prompt_yes_no("Replace it?", default=False):
            print("Setup complete. Using existing key.")
            return 0

    # Prompt for new key
    print("\nGet your API key from: https://console.anthropic.com/")
    print("(Create an account if you don't have one)\n")

    try:
        api_key = input("Enter your Anthropic API key: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled.")
        return 1

    if not api_key:
        print("Error: API key cannot be empty.")
        return 1

    # Validate key format
    if not api_key.startswith("sk-"):
        print("\nWarning: API key usually starts with 'sk-'.")
        if not prompt_yes_no("Save anyway?", default=False):
            print("Setup cancelled.")
            return 1

    # Determine where to save
    env_path = Path.cwd() / ".env"

    # Read existing .env content if it exists
    if env_path.exists():
        content = env_path.read_text()
        if "ANTHROPIC_API_KEY" in content:
            # Replace existing key
            content = re.sub(
                r'ANTHROPIC_API_KEY=.*',
                f'ANTHROPIC_API_KEY={api_key}',
                content
            )
        else:
            # Append to existing file
            content = content.rstrip() + f"\nANTHROPIC_API_KEY={api_key}\n"
    else:
        content = f"ANTHROPIC_API_KEY={api_key}\n"

    # Save
    try:
        env_path.write_text(content)
    except Exception as e:
        print(f"Error saving to {env_path}: {e}")
        print(f"\nYou can manually set the key:")
        print(f"  export ANTHROPIC_API_KEY={api_key}")
        return 1

    print(f"\nAPI key saved to: {env_path}")
    print("Setup complete!")
    print("\nYou can now run:")
    print('  nl2protocol -i "Transfer 100uL from A1 to B1" -c lab_config.json')
    return 0


def offer_setup_on_missing_key() -> bool:
    """
    Offer to run setup when API key is missing.

    Returns:
        True if setup was run successfully, False otherwise
    """
    print("\nNo API key found.")

    # Check if we're in an interactive terminal
    if not sys.stdin.isatty():
        print("Run 'nl2protocol --setup' to configure your API key.")
        return False

    if prompt_yes_no("Would you like to set it up now?", default=True):
        return handle_setup() == 0
    else:
        print("\nTo set up later, run: nl2protocol --setup")
        print("Or manually set: export ANTHROPIC_API_KEY=your-key-here")
        return False


def handle_robot_upload(protocol_path: str) -> bool:
    """
    Handle interactive robot upload flow.

    Returns:
        True if successful or skipped, False if error occurred.
    """
    from .robot import (
        RobotClient,
        load_robot_config,
        save_robot_config,
        create_robot_from_config
    )

    print()  # Blank line for readability

    if not prompt_yes_no("Upload to robot?", default=True):
        return True  # User declined, not an error

    # Get robot config path
    default_config = "robot_config.json"
    config_path = input(f"Robot config path [{default_config}]: ").strip()
    if not config_path:
        config_path = default_config

    # Load or create config
    config = load_robot_config(config_path)

    if config is None:
        print(f"Config '{config_path}' not found. Let's create one.")

        robot_ip = input("Robot IP address: ").strip()
        if not robot_ip:
            print("Error: Robot IP is required.", file=sys.stderr)
            return False

        robot_name = input("Robot name (optional): ").strip() or None

        config = {
            "robot_ip": robot_ip,
            "robot_name": robot_name
        }

        if save_robot_config(config, config_path):
            print(f"Saved config to: {config_path}")
        else:
            print("Warning: Could not save config file.")

    # Create client and connect
    robot = RobotClient(
        ip=config['robot_ip'],
        name=config.get('robot_name'),
        demo_mode=config.get('demo_mode', False)
    )

    print(f"Connecting to {robot.name} ({robot.ip})...")

    try:
        robot.health_check(raise_on_error=True)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False

    print("Connected!")

    # Upload protocol
    print("Uploading protocol...")
    try:
        protocol_id = robot.upload_protocol(protocol_path)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return False

    if not protocol_id:
        print("Error: Failed to upload protocol (no ID returned).", file=sys.stderr)
        return False

    print(f"Protocol uploaded. ID: {protocol_id}")

    # Ask to start run
    if prompt_yes_no("Start run now?", default=True):
        run_id = robot.create_run(protocol_id)
        if not run_id:
            print("Error: Failed to create run.", file=sys.stderr)
            return False

        if robot.start_run(run_id):
            print(f"Run started!")
            print(f"Monitor at: {robot.get_run_url(run_id)}")
        else:
            print("Error: Failed to start run.", file=sys.stderr)
            return False

    return True


def main(argv: list = None) -> int:
    """Main CLI entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)

    # Handle --version
    if args.version:
        from . import __version__
        print(f"nl2protocol {__version__}")
        return 0

    # Handle --setup
    if args.setup:
        return handle_setup()

    # Handle --init
    if args.init:
        return handle_init()

    # Handle --validate-only
    if args.validate_only:
        from .validate_config import validate_config_file
        result = validate_config_file(args.config)
        if result.valid:
            print(f"Config '{args.config}' is valid.")
            return 0
        else:
            print(result, file=sys.stderr)
            return 1

    # For protocol generation, --intent is required
    if not args.intent:
        print("Error: -i/--intent is required for protocol generation", file=sys.stderr)
        parser.print_usage(sys.stderr)
        return 1

    try:
        validate_args(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Resolve intent (read from file if .txt or .pdf)
    try:
        intent = resolve_intent(args.intent)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Apply timestamp to output path
    output_path = get_timestamped_output_path(args.output)

    # Import here to allow --help to work without dependencies
    from .app import ProtocolAgent
    from .errors import NL2ProtocolError, APIKeyError, ConfigFileError

    # Handle --generate-config mode (deprecated)
    config_path = args.config
    if args.generate_config:
        from .colors import warning as cwarn
        print(f"\n  {cwarn('Note:')} --generate-config is deprecated and may produce unreliable configs.", file=sys.stderr)
        print(f"  Recommended: create a config.json describing your actual equipment,", file=sys.stderr)
        print(f"  then use -c config.json. See .env.example and config_examples/.\n", file=sys.stderr)
        from .config_generator import ConfigGenerator, format_config_for_display
        import tempfile

        print("Inferring lab configuration from your instruction...")
        try:
            generator = ConfigGenerator()
            generated_config = generator.generate(intent)
        except APIKeyError as e:
            # Offer interactive setup if key is missing
            if offer_setup_on_missing_key():
                # Retry with new key
                try:
                    from dotenv import load_dotenv
                    load_dotenv(override=True)  # Reload .env
                    generator = ConfigGenerator()
                    generated_config = generator.generate(intent)
                except APIKeyError:
                    print("API key still not working. Please check your key.", file=sys.stderr)
                    return 1
            else:
                return 1

        if generated_config is None:
            print("Error: Could not infer lab configuration from your instruction.", file=sys.stderr)
            print("  The LLM could not determine what labware, pipettes, or modules you need.", file=sys.stderr)
            print("  Try: include specific equipment or volumes in your instruction.", file=sys.stderr)
            print("  Or: provide a config file with -c instead of using --generate-config.", file=sys.stderr)
            return 1

        # Display config to user
        print(format_config_for_display(generated_config))
        print("\nYou must arrange your equipment exactly as shown above.")
        print("Ensure you have all the listed labware, pipettes, and modules.\n")

        if not prompt_yes_no("Proceed with this configuration?", default=True):
            print("Aborted.")
            return 0

        # Save to temp file for use
        temp_config = tempfile.NamedTemporaryFile(
            mode='w', suffix='.json', delete=False
        )
        json.dump(generated_config, temp_config, indent=2)
        temp_config.close()
        config_path = temp_config.name
        print(f"\nUsing generated config: {config_path}")

    try:
        agent = ProtocolAgent(config_path=config_path)
    except APIKeyError as e:
        # Offer interactive setup if key is missing
        if offer_setup_on_missing_key():
            # Retry with new key
            try:
                from dotenv import load_dotenv
                load_dotenv(override=True)  # Reload .env
                agent = ProtocolAgent(config_path=config_path)
            except APIKeyError:
                print("API key still not working. Please check your key.", file=sys.stderr)
                return 1
        else:
            return 1
    except ConfigFileError as e:
        print(str(e), file=sys.stderr)
        return 1
    except NL2ProtocolError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        result = agent.run_pipeline(
            prompt=intent,
            csv_path=args.data,
            max_retries=args.retries
        )
    except NL2ProtocolError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1

    if result is None:
        print(f"\nProtocol generation did not succeed.", file=sys.stderr)
        print(f"  Check the stage-by-stage output above for the specific failure.", file=sys.stderr)
        print(f"  Common fixes:", file=sys.stderr)
        print(f"    - Add more detail (volumes, well positions, labware names)", file=sys.stderr)
        print(f"    - Check your config.json matches your actual equipment", file=sys.stderr)
        print(f"    - Try a simpler instruction first to verify your setup", file=sys.stderr)
        return 1

    # Save protocol
    with open(output_path, 'w') as f:
        f.write(result.script)

    # Save simulation log alongside protocol
    log_path = get_simulation_log_path(output_path)
    with open(log_path, 'w') as f:
        f.write(result.simulation_log)

    # End-of-run summary
    from collections import Counter
    cmd_counts = Counter(c.command_type for c in result.protocol_schema.commands)
    cmd_summary = ", ".join(f"{v}x {k}" for k, v in cmd_counts.most_common())
    slots = sorted(set(lw.slot for lw in result.protocol_schema.labware))
    pipettes = ", ".join(f"{p.model} ({p.mount})" for p in result.protocol_schema.pipettes)

    from .colors import success, label as clabel, dim as cdim

    print(f"\n{'─' * 48}", file=sys.stderr)
    print(f"  {success('Protocol generated successfully.')}", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"  {clabel('Commands:')}   {len(result.protocol_schema.commands)} ({cmd_summary})", file=sys.stderr)
    print(f"  {clabel('Labware:')}    {len(result.protocol_schema.labware)} items (slots {', '.join(slots)})", file=sys.stderr)
    print(f"  {clabel('Pipettes:')}   {pipettes}", file=sys.stderr)
    print(f"  {clabel('Simulation:')} {success('passed')}", file=sys.stderr)
    print(f"  Output:     {output_path}", file=sys.stderr)
    print(f"  Log:        {log_path}", file=sys.stderr)
    print(f"{'─' * 48}", file=sys.stderr)

    # Also print paths to stdout (machine-parseable)
    print(output_path)
    print(log_path)

    # Robot upload flow (if --robot flag is set)
    if args.robot:
        upload_result = handle_robot_upload(output_path)
        if upload_result is False:
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
