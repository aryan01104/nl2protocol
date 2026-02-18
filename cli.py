#!/usr/bin/env python3
"""
Command-line interface for the BioLab Protocol Generator.

Usage:
    python cli.py -i "Your protocol description" [options]

Options:
    -c, --config    Lab configuration JSON file (default: lab_config.json)
    -d, --data      CSV data file with experiment parameters
    -o, --output    Output protocol path (default: generated_protocol.py)
    -r, --retries   Max retry attempts (default: 3)
    --robot         After successful simulation, prompt to upload to robot
"""
import argparse
import sys
import os
from pathlib import Path


def create_parser() -> argparse.ArgumentParser:
    """Create and configure the argument parser."""
    parser = argparse.ArgumentParser(
        prog='biolab',
        description='Generate and verify Opentrons OT-2 protocols from natural language descriptions.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  %(prog)s -i "Transfer 100uL from A1 to B1"
  %(prog)s -i "Perform serial dilution" -d data.csv -o dilution_protocol.py
  %(prog)s -i "Mix samples" -c my_lab.json -r 5

Compatibility:
  - Opentrons OT-2 robot
  - API version 2.15
        '''
    )

    parser.add_argument(
        '-i', '--intent',
        required=True,
        help='Natural language description of the protocol (required)'
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
        help='Output path for generated protocol (default: generated_protocol.py)'
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

    return parser


def validate_args(args: argparse.Namespace) -> None:
    """Validate CLI arguments and raise errors for invalid inputs."""
    if not os.path.isfile(args.config):
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


def prompt_yes_no(question: str, default: bool = True) -> bool:
    """Prompt user for yes/no input."""
    suffix = "[Y/n]" if default else "[y/N]"
    response = input(f"{question} {suffix}: ").strip().lower()
    if not response:
        return default
    return response in ('y', 'yes')


def handle_robot_upload(protocol_path: str) -> bool:
    """
    Handle interactive robot upload flow.

    Returns:
        True if successful or skipped, False if error occurred.
    """
    from robot import (
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
        name=config.get('robot_name')
    )

    print(f"Connecting to {robot.name} ({robot.ip})...")

    if not robot.health_check():
        print(f"Error: Cannot reach robot at {robot.ip}", file=sys.stderr)
        return False

    print("Connected!")

    # Upload protocol
    print("Uploading protocol...")
    protocol_id = robot.upload_protocol(protocol_path)

    if not protocol_id:
        print("Error: Failed to upload protocol.", file=sys.stderr)
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

    try:
        validate_args(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Import here to allow --help to work without dependencies
    from app import ProtocolAgent

    agent = ProtocolAgent(config_path=args.config)

    result = agent.run_pipeline(
        prompt=args.intent,
        csv_path=args.data,
        max_retries=args.retries
    )

    if result is None:
        print(f"\nFailed to generate valid protocol after {args.retries} attempts.", file=sys.stderr)
        return 1

    script, simulation_log = result

    # Save protocol
    with open(args.output, 'w') as f:
        f.write(script)
    print(f"\nProtocol saved to: {args.output}")

    # Save simulation log alongside protocol
    log_path = get_simulation_log_path(args.output)
    with open(log_path, 'w') as f:
        f.write(simulation_log)
    print(f"Simulation log saved to: {log_path}")

    # Robot upload flow (if --robot flag is set)
    if args.robot:
        upload_result = handle_robot_upload(args.output)
        if upload_result is False:
            return 1

    return 0


if __name__ == '__main__':
    sys.exit(main())
