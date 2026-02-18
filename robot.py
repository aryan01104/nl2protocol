"""
Opentrons Robot HTTP API Client

Handles communication with Opentrons OT-2/Flex robots via their HTTP API.
"""

import json
import requests
from pathlib import Path
from typing import Optional, Dict, Any


class RobotClient:
    """Client for communicating with Opentrons robots via HTTP API."""

    API_VERSION = "3"

    def __init__(self, ip: str, name: Optional[str] = None):
        self.ip = ip
        self.name = name or f"Robot@{ip}"
        self.base_url = f"http://{ip}:31950"
        self.headers = {"Opentrons-Version": self.API_VERSION}

    def health_check(self) -> bool:
        """Check if robot is reachable and healthy."""
        try:
            response = requests.get(
                f"{self.base_url}/health",
                headers=self.headers,
                timeout=5
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def get_robot_info(self) -> Optional[Dict[str, Any]]:
        """Get robot information."""
        try:
            response = requests.get(
                f"{self.base_url}/health",
                headers=self.headers,
                timeout=5
            )
            if response.status_code == 200:
                return response.json()
            return None
        except requests.RequestException:
            return None

    def upload_protocol(self, protocol_path: str) -> Optional[str]:
        """
        Upload a protocol file to the robot.

        Returns:
            Protocol ID if successful, None otherwise.
        """
        path = Path(protocol_path)
        if not path.exists():
            raise FileNotFoundError(f"Protocol file not found: {protocol_path}")

        try:
            with open(path, 'rb') as f:
                files = {'protocolFile': (path.name, f, 'application/octet-stream')}
                response = requests.post(
                    f"{self.base_url}/protocols",
                    headers=self.headers,
                    files=files,
                    timeout=30
                )

            if response.status_code in (200, 201):
                data = response.json()
                return data.get('data', {}).get('id')
            else:
                print(f"Upload failed: {response.status_code} - {response.text}")
                return None

        except requests.RequestException as e:
            print(f"Upload error: {e}")
            return None

    def create_run(self, protocol_id: str) -> Optional[str]:
        """
        Create a run from an uploaded protocol.

        Returns:
            Run ID if successful, None otherwise.
        """
        try:
            response = requests.post(
                f"{self.base_url}/runs",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"data": {"protocolId": protocol_id}},
                timeout=10
            )

            if response.status_code in (200, 201):
                data = response.json()
                return data.get('data', {}).get('id')
            else:
                print(f"Create run failed: {response.status_code} - {response.text}")
                return None

        except requests.RequestException as e:
            print(f"Create run error: {e}")
            return None

    def start_run(self, run_id: str) -> bool:
        """Start a created run."""
        try:
            response = requests.post(
                f"{self.base_url}/runs/{run_id}/actions",
                headers={**self.headers, "Content-Type": "application/json"},
                json={"data": {"actionType": "play"}},
                timeout=10
            )
            return response.status_code in (200, 201)

        except requests.RequestException as e:
            print(f"Start run error: {e}")
            return False

    def get_run_status(self, run_id: str) -> Optional[str]:
        """Get the current status of a run."""
        try:
            response = requests.get(
                f"{self.base_url}/runs/{run_id}",
                headers=self.headers,
                timeout=5
            )

            if response.status_code == 200:
                data = response.json()
                return data.get('data', {}).get('status')
            return None

        except requests.RequestException:
            return None

    def get_run_url(self, run_id: str) -> str:
        """Get the URL to monitor a run."""
        return f"{self.base_url}/runs/{run_id}"


def load_robot_config(config_path: str = "robot_config.json") -> Optional[Dict[str, Any]]:
    """Load robot configuration from JSON file."""
    path = Path(config_path)
    if not path.exists():
        return None

    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Error loading robot config: {e}")
        return None


def save_robot_config(config: Dict[str, Any], config_path: str = "robot_config.json") -> bool:
    """Save robot configuration to JSON file."""
    try:
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)
        return True
    except IOError as e:
        print(f"Error saving robot config: {e}")
        return False


def create_robot_from_config(config_path: str = "robot_config.json") -> Optional[RobotClient]:
    """Create a RobotClient from a config file."""
    config = load_robot_config(config_path)
    if not config:
        return None

    ip = config.get('robot_ip')
    if not ip:
        print("Robot config missing 'robot_ip' field")
        return None

    return RobotClient(ip=ip, name=config.get('robot_name'))
