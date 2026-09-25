import tempfile
import unittest
from pathlib import Path

from grokgeneral.service import GrokGeneral


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()
        for index in range(12):
            path = self.root / f"project-{index}"
            path.mkdir()
            (path / "README.md").write_text("# fixture\n", encoding="utf-8")
            self.service.projects.add({"id": f"project-{index}", "name": f"Project {index}", "path": str(path), "priority": "core" if index < 2 else "maintained"})
        self.task = self.service.add_task({"id": "running-task", "goal": "fix failing test", "project": "project-0", "status": "running"})
        self.execution = self.service.executions.start({"task_id": self.task.id, "project": "project-0", "resource": "local"})
        self.service.executions.complete(self.execution["id"], {"status": "failed", "error": "temporary", "raw_events": [{"text": "private output"}]})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_status_snapshot_is_read_only_and_bounded(self):
        first = self.service.status_snapshot()
        second = self.service.status_snapshot()
        self.assertEqual(first["projects"]["items"], second["projects"]["items"])
        self.assertEqual(first["tasks_by_status"], second["tasks_by_status"])
        self.assertLessEqual(len(first["projects"]["items"]), 10)
        self.assertNotIn("raw_log_path", str(first))
        self.assertNotIn("private output", str(first))

    def test_status_snapshot_contains_daily_dashboard_fields(self):
        value = self.service.status_snapshot()
        for key in ("health", "blockers", "running_tasks", "pending_approvals", "free_resources", "expiring_resources", "recent_completions", "top_opportunities", "next_actions", "usage"):
            self.assertIn(key, value)

    def test_full_status_is_bounded_but_more_detailed(self):
        value = self.service.status_snapshot(full=True)
        self.assertIn("project_details", value)
        self.assertLessEqual(len(value["project_details"]), 50)
