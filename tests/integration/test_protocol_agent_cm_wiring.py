"""
Integration test: ProtocolAgent correctly accepts and routes through a
ConfirmationManager. This verifies the Phase A refactor (orchestration vs UX
separation) wired the manager correctly without breaking the constructor.

Scope:
  - Default constructor → uses InteractiveCM
  - Explicit ScriptedCM → stored on the agent and routes prompts through it
  - Explicit AutoConfirmCM → returns empty strings
  - Phase B (future): drive run_pipeline() end-to-end with a ScriptedCM and
    mocked Anthropic client. Deferred because run_pipeline is still tightly
    coupled to side effects (file I/O for state log, _log/print for UI) that
    are not in the Phase A scope.
"""

import pytest

from nl2protocol.confirmation import (
    AutoConfirmCM,
    InteractiveCM,
    ScriptedCM,
)


@pytest.fixture(autouse=True)
def stub_api_key(monkeypatch):
    """ProtocolAgent's ConfigLoader requires ANTHROPIC_API_KEY at construction."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-stub-key")


class TestProtocolAgentCMWiring:
    """Verifies the ConfirmationManager dependency-injection wiring on ProtocolAgent."""

    def _agent(self, **kwargs):
        from nl2protocol.pipeline import ProtocolAgent
        return ProtocolAgent(
            config_path="test_cases/examples/simple_transfer/config.json",
            **kwargs,
        )

    # Post: default constructor uses InteractiveCM
    def test_default_constructor_uses_interactive_cm(self):
        agent = self._agent()
        assert isinstance(agent.cm, InteractiveCM)

    # Post: explicit ScriptedCM is stored verbatim and prompts route through it
    def test_scripted_cm_routes_prompts(self):
        scripted = ScriptedCM(["yes", "1", ""])
        agent = self._agent(confirmation_manager=scripted)
        assert agent.cm is scripted

        # Verify prompts route through ScriptedCM (FIFO).
        assert agent.cm.prompt("first?") == "yes"
        assert agent.cm.prompt("second?") == "1"
        assert agent.cm.prompt("third?") == ""

    # Post: AutoConfirmCM returns empty strings for any question
    def test_auto_confirm_cm_routes_to_empty(self):
        auto = AutoConfirmCM()
        agent = self._agent(confirmation_manager=auto)
        assert agent.cm is auto
        assert agent.cm.prompt("Continue?") == ""

    # Post: ScriptedCM exhaustion raises with helpful context (script-ran-short bug surfaces)
    def test_scripted_cm_exhaustion_surfaces_clearly(self):
        scripted = ScriptedCM(["only_one"])
        agent = self._agent(confirmation_manager=scripted)
        agent.cm.prompt("first")  # OK
        with pytest.raises(IndexError, match="ScriptedCM exhausted"):
            agent.cm.prompt("second")
