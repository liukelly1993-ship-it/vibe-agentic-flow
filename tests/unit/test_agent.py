import unittest

from vaf.agents.fake_agent import FakeAgent


class FakeAgentTests(unittest.TestCase):
    def test_rejects_implementation_path_escape(self) -> None:
        with self.assertRaises(ValueError):
            FakeAgent().generate_code(
                change_id="CHG-001",
                title="demo",
                objective="demo",
                implementation={
                    "changes": [
                        {"task_id": "TASK-001", "path": "../outside.py", "content": "VALUE = 1\n"}
                    ]
                },
            )
