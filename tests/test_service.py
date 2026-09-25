import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.service import GrokGeneral


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "package.json").write_text("{}", encoding="utf-8")
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_initialize_and_status_are_offline_and_use_seed_resource(self):
        self.service.scan_projects(self.root)
        status = self.service.status()
        self.assertEqual(status["projects"], 1)
        self.assertGreaterEqual(status["resources"], 1)
        self.assertIn("space-bunny", [item["id"] for item in status["resource_summary"]])
        self.assertEqual(status["health"]["ok"], True)

    def test_route_context_ask_and_opportunities(self):
        self.service.scan_projects(self.root)
        task = self.service.add_task({"goal": "audit repo tests", "project": "repo", "required_capabilities": ["repo-analysis", "testing"]})
        route = self.service.route_task(task.id)
        self.assertNotEqual(route["status"], "blocked")
        self.assertIsNotNone(route["routing_rationale"].get("context_pack"))
        context = self.service.context(task.id)
        self.assertEqual(context["task"]["id"], task.id)
        answer = self.service.ask("What should Space Bunny work on this week?")
        self.assertFalse(answer["ai_used"])
        self.assertIn("space-bunny", json.dumps(answer).lower())
        opportunities = self.service.opportunities()
        self.assertTrue(opportunities)

    def test_export_import_and_close(self):
        snapshot = self.service.export_snapshot()
        destination = GrokGeneral(self.root / "imported")
        destination.initialize()
        destination.import_snapshot(snapshot)
        self.assertEqual(len(destination.projects.list()), 0)
        destination.close()


if __name__ == "__main__":
    unittest.main()
