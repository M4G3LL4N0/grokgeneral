import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from grokgeneral.cli import main
from grokgeneral.service import GrokGeneral


class RefreshCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "README.md").write_text("# fixture\n", encoding="utf-8")
        (self.repo / "LICENSE").write_text("fixture", encoding="utf-8")
        (self.repo / "src" / "index.js").write_text("// TODO: finish parser\n", encoding="utf-8")
        (self.repo / "test.js").write_text("test", encoding="utf-8")
        (self.repo / "package.json").write_text('{"scripts":{"build":"true","test":"true"}}', encoding="utf-8")
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        (self.repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()
        self.project = self.service.projects.add({"id": "fixture", "name": "Fixture", "path": str(self.repo), "kind": "tool", "priority": "core", "status": "active"})
        self.state_dir = str(self.root / "state")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def run_cli(self, *argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(argv))
        return code, buffer.getvalue()

    def run_json(self, *argv):
        code, output = self.run_cli("--json", *argv)
        return code, json.loads(output)

    def test_status_is_read_only_and_reports_freshness(self):
        code, value = self.run_json("status", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["health_freshness"]["never_scanned"], 1)
        self.assertEqual(value["opportunity_digest"]["status"], "never_refreshed")

    def test_backlog_scan_persists_summaries_and_backlog_reads_them(self):
        code, value = self.run_json("backlog", "scan", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["scanned"], 1)
        self.assertEqual(value["opportunity_digest"]["status"], "fresh")
        code, value = self.run_json("backlog", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["source"], "persisted_summaries")
        self.assertEqual(value["projects"][0]["project"], "fixture")
        self.assertEqual(value["projects"][0]["backlog_count"], 1)
        self.assertTrue(value["projects"][0]["last_scanned"])

    def test_backlog_scan_can_skip_opportunity_digest(self):
        self.service.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "model": "opencode/space-bunny-free", "cost_class": "free", "availability": "unlimited", "capabilities": ["coding", "testing"]})
        code, value = self.run_json("backlog", "scan", "--no-opportunities", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["opportunity_digest"]["status"], "never_refreshed")

    def test_backlog_live_scan_is_explicit(self):
        code, value = self.run_json("backlog", "scan", "--live", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["source"], "live_inspection")
        self.assertTrue(value["findings"])

    def test_backlog_project_filter_reads_persisted_summary(self):
        self.run_json("backlog", "scan", "--state-dir", self.state_dir)
        code, value = self.run_json("backlog", "--project", "fixture", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["source"], "persisted_summaries")
        self.assertEqual(len(value["projects"]), 1)

    def test_opportunities_refresh_records_digest(self):
        code, value = self.run_json("opportunities", "refresh", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertEqual(value["recorded"], True)
        self.assertIn("generated_at", value)
        code, value = self.run_json("status", "--state-dir", self.state_dir)
        self.assertEqual(value["opportunity_digest"]["status"], "fresh")

    def test_projects_scan_refreshes_health_summaries(self):
        self.service.roots.add(self.root)
        self.service.close()
        code, value = self.run_json("projects", "scan", "--root", str(self.root), "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertIsInstance(value, list)
        self.assertEqual([item["id"] for item in value], ["fixture"])
        self.service = GrokGeneral(self.root / "state")
        self.assertIsNotNone(self.service.status_model.summary("fixture")["last_scanned"])

    def test_projects_scan_can_skip_health_refresh(self):
        self.service.roots.add(self.root)
        self.service.close()
        self.run_json("projects", "scan", "--root", str(self.root), "--no-health", "--state-dir", self.state_dir)
        self.service = GrokGeneral(self.root / "state")
        self.assertIsNone(self.service.status_model.summary("fixture")["last_scanned"])

    def test_status_text_output_mentions_health_freshness(self):
        self.run_json("backlog", "scan", "--state-dir", self.state_dir)
        code, output = self.run_cli("status", "--state-dir", self.state_dir)
        self.assertEqual(code, 0)
        self.assertIn("Health", output)


if __name__ == "__main__":
    unittest.main()
