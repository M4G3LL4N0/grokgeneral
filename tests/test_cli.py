import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from grokgeneral.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.temp.name) / "state")
        self.root = Path(self.temp.name) / "startups"
        self.root.mkdir()
        repo = self.root / "repo"
        repo.mkdir()
        (repo / "package.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--state-dir", self.state, *args])
        return code, output.getvalue(), errors.getvalue()

    def test_json_commands_are_machine_readable(self):
        code, output, errors = self.call("projects", "scan", "--root", str(self.root), "--json")
        self.assertEqual(code, 0, errors)
        value = json.loads(output)
        self.assertEqual(len(value), 1)
        code, output, errors = self.call("resources", "--json")
        self.assertEqual(code, 0, errors)
        self.assertTrue(any(item["id"] == "space-bunny" for item in json.loads(output)))

    def test_task_add_route_and_context_json(self):
        self.call("projects", "scan", "--root", str(self.root))
        code, output, errors = self.call("task", "add", "audit tests", "--project", "repo", "--capabilities", "repo-analysis,testing", "--json")
        self.assertEqual(code, 0, errors)
        task = json.loads(output)
        code, output, errors = self.call("task", "route", task["id"], "--json")
        self.assertEqual(code, 0, errors)
        self.assertIn("assigned_executor", json.loads(output))
        code, output, errors = self.call("context", task["id"], "--json")
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(output)["task"]["id"], task["id"])

    def test_route_goal_supports_project_and_json(self):
        self.call("projects", "scan", "--root", str(self.root))
        code, output, errors = self.call("route", "audit repo", "--project", "repo", "--json")
        self.assertEqual(code, 0, errors)
        self.assertIn("executor", json.loads(output))

    def test_errors_have_nonzero_exit_codes_and_json_shape(self):
        code, output, errors = self.call("project", "show", "missing", "--json")
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output)["error"], "project not found: missing")
        code, output, errors = self.call("task", "run", "missing", "--json")
        self.assertEqual(code, 3)
        self.assertIn("task not found", json.loads(output)["error"])

    def test_sensitive_task_execution_is_blocked_by_default(self):
        code, output, errors = self.call("task", "add", "fetch", "--command", '["curl", "https://example.com"]', "--json")
        self.assertEqual(code, 0, errors)
        task = json.loads(output)
        code, output, errors = self.call("task", "run", task["id"], "--json")
        self.assertEqual(code, 4)
        self.assertIn("network", json.loads(output)["error"])

    def test_status_doctor_and_resource_event_json(self):
        code, output, errors = self.call("status", "--json")
        self.assertEqual(code, 0, errors)
        self.assertIn("next_actions", json.loads(output))
        code, output, errors = self.call("doctor", "--json")
        self.assertEqual(code, 0, errors)
        self.assertIsInstance(json.loads(output), list)
        code, output, errors = self.call("event", "emit", "RESOURCE_AVAILABLE", "--resource", "space-bunny", "--expires", "7d", "--json")
        self.assertEqual(code, 0, errors)
        self.assertIn("opportunities", json.loads(output))

    def test_optimize_is_plan_only_without_execute(self):
        code, output, errors = self.call("optimize", "--json")
        self.assertEqual(code, 0, errors)
        value = json.loads(output)
        self.assertFalse(value["executed"])


if __name__ == "__main__":
    unittest.main()
