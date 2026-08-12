import tempfile
import unittest
from pathlib import Path

from vaf.policy.engine import PolicyDecisionType, PolicyEngine, ToolRequest


class PolicyEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = PolicyEngine()
        self.workspace = Path(tempfile.mkdtemp())

    def test_allow_whitelisted_command(self) -> None:
        decision = self.engine.evaluate(
            ToolRequest("run_command", {"argv": ["pytest", "-q"]}, self.workspace)
        )
        self.assertEqual(decision.decision, PolicyDecisionType.ALLOW)

    def test_deny_unknown_command(self) -> None:
        decision = self.engine.evaluate(
            ToolRequest("run_command", {"argv": ["rm", "-rf", "."]}, self.workspace)
        )
        self.assertEqual(decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(decision.rule_id, "VAF-POLICY-COMMAND")

    def test_deny_path_escape(self) -> None:
        decision = self.engine.evaluate(
            ToolRequest("write_file", {"path": "../outside.txt"}, self.workspace)
        )
        self.assertEqual(decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(decision.rule_id, "VAF-POLICY-PATH")

    def test_deny_network_and_secret(self) -> None:
        for tool_name in ("network_request", "read_secret", "deploy_production"):
            with self.subTest(tool_name=tool_name):
                decision = self.engine.evaluate(ToolRequest(tool_name, {}, self.workspace))
                self.assertEqual(decision.decision, PolicyDecisionType.DENY)

    def test_explicit_dependency_install_network_mode_can_be_scoped(self) -> None:
        command = ("docker", "compose", "up", "--build")
        policy = PolicyEngine(
            allowed_commands={command},
            allowed_network_modes={"disabled", "dependency-install"},
        )
        decision = policy.evaluate(
            ToolRequest(
                tool_name="run_command",
                args={"argv": list(command)},
                workspace_root=self.workspace,
                network_mode="dependency-install",
            )
        )
        self.assertEqual(decision.decision, PolicyDecisionType.ALLOW)
