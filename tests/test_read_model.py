import tempfile
import unittest
from pathlib import Path

from grokgeneral.service import GrokGeneral
from grokgeneral.storage import canonical_json
from grokgeneral.timeutil import isoformat, utc_now


class StatusReadModelTests(unittest.TestCase):
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
        self.calls = []
        original = self.service.backlog.inspect

        def counting(project=None):
            self.calls.append(getattr(project, "id", project))
            return original(project)

        self.service.backlog.inspect = counting

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_summary_before_any_scan_reports_never_scanned(self):
        summary = self.service.status_model.summary("fixture")
        self.assertIsNone(summary["last_scanned"])
        self.assertEqual(summary["freshness"], "never")
        self.assertTrue(summary["stale"])
        self.assertIsNone(summary["age_seconds"])
        self.assertEqual(summary["git_health"], "unknown")
        self.assertIsNone(summary["dirty"])
        self.assertEqual(self.calls, [])

    def test_explicit_scan_persists_health_fields(self):
        result = self.service.refresh_status()
        self.assertEqual(result["scanned"], 1)
        summary = self.service.status_model.summary("fixture")
        for field in ("last_scanned", "git_health", "dirty", "test_status", "build_status", "ci_present", "blockers", "backlog_count", "recent_execution"):
            self.assertIn(field, summary)
        self.assertIsNotNone(summary["last_scanned"])
        self.assertEqual(summary["freshness"], "fresh")
        self.assertFalse(summary["stale"])
        self.assertTrue(summary["ci_present"])
        self.assertEqual(summary["backlog_count"], 1)
        self.assertEqual(summary["blockers"], [])
        self.assertIsNone(summary["recent_execution"])

    def test_summary_read_never_rescans(self):
        self.service.refresh_status()
        self.calls.clear()
        for _ in range(5):
            self.service.status_model.summary("fixture")
            self.service.status_model.summaries()
            self.service.status_model.health_overview()
            self.service.status_snapshot()
        self.assertEqual(self.calls, [])

    def test_status_snapshot_does_not_scan_or_rebuild_opportunities(self):
        report = self.service.refresh_status()
        self.calls.clear()
        snapshot = self.service.status_snapshot()
        self.assertEqual(self.calls, [])
        self.assertEqual(snapshot["opportunity_digest"]["generated_at"], report["opportunity_digest"]["generated_at"])
        self.assertEqual(snapshot["health_freshness"]["scanned"], 1)
        self.assertEqual(snapshot["health_freshness"]["never_scanned"], 0)
        self.assertEqual(snapshot["health_freshness"]["stale"], 0)

    def test_status_snapshot_without_refresh_reports_missing_digest(self):
        snapshot = self.service.status_snapshot()
        self.assertEqual(snapshot["opportunity_digest"]["status"], "never_refreshed")
        self.assertEqual(snapshot["top_opportunities"], [])
        self.assertEqual(snapshot["health_freshness"]["never_scanned"], 1)

    def test_stale_summaries_are_flagged_past_max_age(self):
        self.service.refresh_status()
        overview = self.service.status_model.health_overview(max_age_seconds=0)
        self.assertEqual(overview["stale"], 1)
        self.assertEqual(overview["fresh"], 0)
        summary = self.service.status_model.summary("fixture", max_age_seconds=0)
        self.assertEqual(summary["freshness"], "stale")
        self.assertTrue(summary["stale"])

    def test_scan_is_incremental_and_skips_fresh_projects(self):
        self.service.refresh_status()
        self.calls.clear()
        result = self.service.refresh_status()
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["skipped_fresh"], 1)
        self.assertEqual(self.calls, [])
        forced = self.service.refresh_status(force=True)
        self.assertEqual(forced["scanned"], 1)
        self.assertEqual(forced["skipped_fresh"], 0)
        self.assertEqual(self.calls, ["fixture"])

    def test_digest_stores_compact_opportunities_not_raw_candidates(self):
        self.service.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "model": "opencode/space-bunny-free", "cost_class": "free", "availability": "unlimited", "capabilities": ["coding", "testing", "repo-analysis"], "expires_at": isoformat(utc_now())})
        self.service.refresh_status()
        items = self.service.status_model.opportunity_digest()["items"]
        self.assertTrue(items)
        for item in items:
            self.assertLessEqual(len(canonical_json(item).encode("utf-8")), 1000)
            for key in ("work_key", "project", "score", "evidence"):
                self.assertIn(key, item)
        self.assertNotIn("previous_executions", items[0])
        self.assertNotIn("proposed_work", items[0])

    def test_digest_records_generated_at_and_bounded_items(self):
        self.service.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "model": "opencode/space-bunny-free", "cost_class": "free", "availability": "unlimited", "capabilities": ["coding", "testing", "repo-analysis"], "expires_at": isoformat(utc_now())})
        self.service.refresh_status()
        digest = self.service.status_model.opportunity_digest()
        self.assertIsNotNone(digest["generated_at"])
        self.assertEqual(digest["status"], "fresh")
        self.assertLessEqual(len(digest["items"]), 10)
        self.assertTrue(all("work_key" in item and "project" in item for item in digest["items"]))

    def test_health_overview_counts_projects_without_summaries(self):
        self.service.projects.add({"id": "second", "name": "Second", "path": str(self.root / "other"), "kind": "tool", "priority": "active"})
        overview = self.service.status_model.health_overview()
        self.assertEqual(overview["projects"], 2)
        self.assertEqual(overview["scanned"], 0)
        self.assertEqual(overview["never_scanned"], 2)

    def test_project_blockers_are_recorded_in_summary(self):
        self.service.projects.update("fixture", {"blockers": ["waiting on API decision"]})
        self.service.refresh_status()
        summary = self.service.status_model.summary("fixture")
        self.assertEqual(summary["blockers"], ["waiting on API decision"])


if __name__ == "__main__":
    unittest.main()
