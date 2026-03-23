"""
Opentrons Robot HTTP API Client

Handles communication with Opentrons OT-2/Flex robots via their HTTP API.
"""

import json
import requests
from pathlib import Path
from typing import Optional, Dict, Any

from .errors import RobotConnectionError


def diagnose_connection_error(ip: str, error: Exception) -> str:
    """Diagnose connection error and return user-friendly message."""
    error_str = str(error).lower()

    if "connection refused" in error_str:
        return (
            f"Connection refused by {ip}.\n"
            "The robot may not be running or the HTTP API is disabled.\n"
            "Try restarting the robot."
        )
    elif "timeout" in error_str or "timed out" in error_str:
        return (
            f"Connection to {ip} timed out.\n"
            "The robot may be powered off or on a different network.\n"
            "Check that your computer and robot are on the same WiFi network."
        )
    elif "no route to host" in error_str or "network is unreachable" in error_str:
        return (
            f"Cannot reach {ip} - network unreachable.\n"
            "Check your network connection and verify the robot's IP address."
        )
    elif "name or service not known" in error_str or "nodename nor servname" in error_str:
        return (
            f"Cannot resolve hostname: {ip}.\n"
            "Use the robot's IP address instead (e.g., 192.168.1.100)."
        )
    else:
        return f"Connection failed: {error}"


class RobotClient:
    """Client for communicating with Opentrons robots via HTTP API."""

    API_VERSION = "3"

    def __init__(self, ip: str, name: Optional[str] = None, demo_mode: bool = False):
        self.ip = ip
        self.name = name or f"Robot@{ip}"
        self.base_url = f"http://{ip}:31950"
        self.headers = {"Opentrons-Version": self.API_VERSION}
        self.demo_mode = demo_mode

        if demo_mode:
            print("[DEMO MODE] Robot operations will be simulated")

    def health_check(self, raise_on_error: bool = False) -> bool:
        """Check if robot is reachable and healthy.

        Args:
            raise_on_error: If True, raise RobotConnectionError with details on failure
        """
        if self.demo_mode:
            print(f"[DEMO MODE] Health check passed for {self.name}")
            return True

        try:
            response = requests.get(
                f"{self.base_url}/health",
                headers=self.headers,
                timeout=5
            )
            if response.status_code == 200:
                return True
            elif raise_on_error:
                raise RobotConnectionError(
                    self.ip,
                    f"Robot returned unexpected status: {response.status_code}"
                )
            return False
        except requests.RequestException as e:
            if raise_on_error:
                raise RobotConnectionError(self.ip, diagnose_connection_error(self.ip, e))
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

        Raises:
            FileNotFoundError: If protocol file doesn't exist
            RobotConnectionError: If upload fails due to connection issues
        """
        path = Path(protocol_path)
        if not path.exists():
            raise FileNotFoundError(f"Protocol file not found: {protocol_path}")

        if self.demo_mode:
            demo_protocol_id = f"demo-protocol-{path.stem}"
            print(f"[DEMO MODE] Protocol '{path.name}' uploaded successfully")
            print(f"[DEMO MODE] Protocol ID: {demo_protocol_id}")
            return demo_protocol_id

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
            elif response.status_code == 400:
                # Protocol validation error
                try:
                    error_data = response.json()
                    error_msg = error_data.get('errors', [{}])[0].get('detail', response.text)
                except:
                    error_msg = response.text
                raise RobotConnectionError(self.ip, f"Protocol rejected by robot: {error_msg}")
            else:
                raise RobotConnectionError(
                    self.ip,
                    f"Upload failed with status {response.status_code}: {response.text[:200]}"
                )

        except requests.RequestException as e:
            raise RobotConnectionError(self.ip, diagnose_connection_error(self.ip, e))

    def create_run(self, protocol_id: str) -> Optional[str]:
        """
        Create a run from an uploaded protocol.

        Returns:
            Run ID if successful, None otherwise.
        """
        if self.demo_mode:
            demo_run_id = f"demo-run-{protocol_id.replace('demo-protocol-', '')}"
            print(f"[DEMO MODE] Run created: {demo_run_id}")
            return demo_run_id

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
        if self.demo_mode:
            print(f"[DEMO MODE] Run started: {run_id}")
            return True

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

    return RobotClient(
        ip=ip,
        name=config.get('robot_name'),
        demo_mode=config.get('demo_mode', False)
    )
