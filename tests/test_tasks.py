import sys
import tempfile
import unittest
from pathlib import Path

from grokgeneral.errors import ValidationError
from grokgeneral.events import EventBus, TASK_CREATED
from grokgeneral.policies import PolicyEngine
from grokgeneral.resources import ResourceRegistry
from grokgeneral.router import Router
from grokgeneral.storage import StateStore
from grokgeneral.tasks import TaskRegistry
from grokgeneral.usage import UsageLedger


class TaskRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.events = EventBus(self.state)
        self.policies = PolicyEngine(self.state)
        self.resources = ResourceRegistry(self.state, self.events)
        self.resources.add({"id": "local", "name": "local", "provider": "local", "executor": "shell", "cost_class": "free", "capabilities": ["testing", "coding"], "availability": "available"})
        self.router = Router(self.state, self.policies, self.resources)
        self.usage = UsageLedger(self.state)
        self.tasks = TaskRegistry(self.state, self.events, self.policies, self.resources, self.router, self.usage)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_add_persists_and_emits_task_created(self):
        task = self.tasks.add({"goal": "run tests", "project": "demo", "required_capabilities": ["testing"]})
        self.assertEqual(task.status, "proposed")
        self.assertEqual(self.tasks.get(task.id).goal, "run tests")
        self.assertTrue(any(event["event_type"] == TASK_CREATED for event in self.events.list()))

    def test_dependencies_must_be_complete_before_run(self):
        first = self.tasks.add({"goal": "first", "dependencies": []})
        second = self.tasks.add({"goal": "second", "dependencies": [first.id]})
        with self.assertRaises(ValidationError):
            self.tasks.run(second.id)
        self.tasks.update(first.id, {"status": "running"})
        self.tasks.update(first.id, {"status": "completed"})
        self.assertEqual(self.tasks.get(second.id).status, "proposed")

    def test_safe_local_command_runs_and_records_output(self):
        project = Path(self.temp.name) / "project"
        project.mkdir()
        task = self.tasks.add({"goal": "safe test", "metadata": {"command": [sys.executable, "-c", "print('ok')"]}, "project": None})
        task = self.tasks.run(task.id, project_path=str(project))
        self.assertEqual(task.status, "completed")
        self.assertIn("ok", task.outputs[0]["stdout"])

    def test_invalid_lifecycle_transition_is_rejected(self):
        task = self.tasks.add({"goal": "done"})
        with self.assertRaises(ValidationError):
            self.tasks.update(task.id, {"status": "completed"})
        task = self.tasks.update(task.id, {"status": "queued"})
        task = self.tasks.update(task.id, {"status": "running"})
        task = self.tasks.complete(task.id, [{"result": "ok"}])
        self.assertEqual(task.status, "completed")

    def test_failed_local_command_counts_one_attempt(self):
        task = self.tasks.add({"goal": "fail command", "metadata": {"command": [sys.executable, "-c", "raise SystemExit(2)"]}})
        task = self.tasks.run(task.id)
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.attempts, 1)

    def test_fail_increments_attempts_and_records_error(self):
        task = self.tasks.add({"goal": "fail"})
        self.tasks.update(task.id, {"status": "running"})
        task = self.tasks.fail(task.id, "broken")
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.attempts, 1)
        self.assertIn("broken", task.metadata["error"])


if __name__ == "__main__":
    unittest.main()
