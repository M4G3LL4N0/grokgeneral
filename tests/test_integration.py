import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.service import GrokGeneral


class IntegrationTests(unittest.TestCase):
    def test_offline_end_to_end_control_plane_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project_path = root / "repo"
            project_path.mkdir()
            (project_path / "package.json").write_text('{"scripts":{"test":"true"}}', encoding="utf-8")
            service = GrokGeneral(root / "state", offline=True)
            service.initialize()
            service.scan_projects(root)
            task = service.add_task({"id": "integration-task", "goal": "audit tests", "project": "repo", "required_capabilities": ["testing"]})
            routed = service.route_task(task.id)
            self.assertEqual(routed["status"], "queued")
            context = service.context(task.id)
            self.assertEqual(context["task"]["id"], task.id)
            self.assertTrue(service.opportunities("space-bunny"))
            event = service.events.emit("TASK_CREATED", {"task_id": task.id, "token": "hidden"})
            self.assertNotIn("hidden", json.dumps(event))
            service.usage.record("measured", units=1, project="repo", resource="local")
            self.assertEqual(service.usage.summary(project="repo")["count"], 1)
            snapshot = service.export_snapshot()
            imported = GrokGeneral(root / "imported", offline=True)
            imported.initialize()
            imported.import_snapshot(snapshot)
            self.assertEqual(imported.tasks.get(task.id).goal, "audit tests")
            imported.close()
            service.close()


if __name__ == "__main__":
    unittest.main()
