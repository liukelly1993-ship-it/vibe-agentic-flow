import tempfile
import unittest
from pathlib import Path

from vaf.adapters.jsonl_event_store import JsonlEventStore
from vaf.adapters.tool_gateway import ToolExecutionResult, ToolGateway
from vaf.policy.engine import PolicyDecisionType, PolicyEngine, ToolRequest
from vaf.ports.tools import ToolExecutionOutput


class FakeTool:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, args: dict[str, object], workspace_root: Path) -> ToolExecutionOutput:
        self.calls += 1
        return ToolExecutionOutput(exit_code=0, stdout="ok")


class ToolGatewayTests(unittest.TestCase):
    def test_denied_request_never_calls_adapter(self) -> None:
        tool = FakeTool()
        gateway = ToolGateway(PolicyEngine(), {"run_command": tool})
        result = gateway.execute(
            ToolRequest("run_command", {"argv": ["rm", "-rf", "."]}, Path.cwd())
        )
        self.assertFalse(result.executed)
        self.assertEqual(result.decision.decision, PolicyDecisionType.DENY)
        self.assertEqual(tool.calls, 0)

    def test_idempotent_request_reuses_completed_result(self) -> None:
        tool = FakeTool()
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(Path(directory) / "events.jsonl")
            gateway = ToolGateway(PolicyEngine(), {"run_command": tool}, store)
            request = ToolRequest(
                "run_command",
                {"argv": ["pytest", "-q"]},
                Path(directory),
                run_id="RUN-001",
                change_id="CHG-001",
                idempotency_key="same-key",
            )
            first = gateway.execute(request)
            second = gateway.execute(request)
        self.assertIsInstance(first, ToolExecutionResult)
        self.assertTrue(second.reused)
        self.assertEqual(tool.calls, 1)
        self.assertEqual(first.output, second.output)

    def test_idempotent_request_reuses_result_across_gateway_instances(self) -> None:
        tool = FakeTool()
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(Path(directory) / "events.jsonl")
            request = ToolRequest(
                "run_command",
                {"argv": ["pytest", "-q"]},
                Path(directory),
                run_id="RUN-001",
                change_id="CHG-001",
                idempotency_key="persisted-key",
            )
            first = ToolGateway(PolicyEngine(), {"run_command": tool}, store).execute(request)
            second_tool = FakeTool()
            second = ToolGateway(PolicyEngine(), {"run_command": second_tool}, store).execute(request)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(second_tool.calls, 0)
        self.assertEqual(first.invocation_id, second.invocation_id)
        self.assertEqual(first.output, second.output)

    def test_gateway_emits_audit_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlEventStore(Path(directory) / "events.jsonl")
            gateway = ToolGateway(PolicyEngine(), {"run_command": FakeTool()}, store)
            gateway.execute(
                ToolRequest(
                    "run_command",
                    {"argv": ["pytest", "-q"]},
                    Path(directory),
                    run_id="RUN-001",
                    change_id="CHG-001",
                )
            )
            events = store.read_all()
        self.assertEqual(
            [event.event_type for event in events],
            ["ToolInvocationRequested", "PolicyDecisionMade", "ToolInvocationCompleted"],
        )
        self.assertEqual(events[-1].payload["stdout"], "ok")
