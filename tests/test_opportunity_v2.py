import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from grokgeneral.service import GrokGeneral
from grokgeneral.timeutil import isoformat, utc_now


class OpportunityV2Tests(unittest.TestCase):
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
        self.service.set_project_validation("fixture", [["python3", "-c", "print('ok')"]])
        self.service.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "model": "opencode/space-bunny-free", "cost_class": "free", "availability": "unlimited", "capabilities": ["coding", "testing", "repo-analysis"], "expires_at": isoformat(utc_now() + timedelta(days=2))})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_no_evidence_returns_no_generic_opportunity(self):
        clean = self.root / "clean"
        clean.mkdir()
        (clean / "README.md").write_text("# clean\n", encoding="utf-8")
        (clean / "LICENSE").write_text("fixture", encoding="utf-8")
        (clean / "package.json").write_text('{"scripts":{"build":"true","test":"true"}}', encoding="utf-8")
        (clean / ".github" / "workflows").mkdir(parents=True)
        (clean / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
        (clean / "test.js").write_text("test", encoding="utf-8")
        self.service.projects.add({"id": "clean", "name": "Clean", "path": str(clean), "kind": "tool", "priority": "maintained"})
        values = [item for item in self.service.opportunities() if item["project"] == "clean"]
        self.assertEqual(values, [])

    def test_concrete_evidence_is_returned_with_deduplication(self):
        first = self.service.opportunities()
        second = self.service.opportunities()
        self.assertTrue(first)
        self.assertEqual(first[0]["project"], "fixture")
        self.assertIn("evidence", first[0])
        self.assertEqual(first[0]["work_key"], second[0]["work_key"])
        self.assertNotEqual(first[0]["work_key"], "")

    def test_blockers_and_validation_visibility_affect_candidate(self):
        self.service.projects.update("fixture", {"blockers": ["waiting for API decision"]})
        values = self.service.opportunities()
        fixture = next(item for item in values if item["project"] == "fixture")
        self.assertFalse(fixture["eligible"])
        self.assertIn("blocker", " ".join(fixture["blocked_reasons"]).lower())
        self.assertTrue(fixture["validation_ready"])

    def test_completed_execution_with_same_work_key_suppresses_candidate(self):
        task = self.service.add_task({"id": "known-task", "goal": "fix failing test", "project": "fixture", "required_capabilities": ["testing"]})
        opportunity = next(item for item in self.service.opportunities() if item["task"] == task.id)
        execution = self.service.executions.start({"task_id": task.id, "project": "fixture", "resource": "space", "work_key": opportunity["work_key"]})
        self.service.executions.complete(execution["id"], {"status": "completed", "validation": {"status": "passed"}})
        values = [item for item in self.service.opportunities() if item["work_key"] == opportunity["work_key"]]
        self.assertEqual(values, [])

    def test_optimize_is_global_bounded_and_queue_only(self):
        second_repo = self.root / "second"
        second_repo.mkdir()
        (second_repo / "README.md").write_text("# second\n", encoding="utf-8")
        (second_repo / "package.json").write_text('{"scripts":{"build":"true","test":"true"}}', encoding="utf-8")
        (second_repo / "test.js").write_text("test", encoding="utf-8")
        (second_repo / "LICENSE").write_text("fixture", encoding="utf-8")
        (second_repo / ".github" / "workflows").mkdir(parents=True)
        (second_repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
        self.service.projects.add({"id": "second", "name": "Second", "path": str(second_repo), "kind": "tool", "priority": "active"})
        self.service.set_project_validation("second", [["python3", "-c", "print('ok')"]])
        first_task = self.service.add_task({"id": "first-task", "goal": "fix failing test", "project": "fixture", "required_capabilities": ["testing"]})
        second_task = self.service.add_task({"id": "second-task", "goal": "repair CI", "project": "second", "required_capabilities": ["repo-analysis"]})
        plan = self.service.optimize(max_tasks=1)
        self.assertEqual(len(plan["items"]), 1)
        self.assertIn(plan["items"][0]["project"], {"fixture", "second"})
        self.assertIn("executor", plan["items"][0])
        self.assertIn("approval_needed", plan["items"][0])
        self.assertFalse(plan["executed"])
        queued = self.service.optimize(max_tasks=2, queue=True)
        self.assertGreaterEqual(queued["queued"], 1)
        self.assertIn(first_task.id, {task.id for task in self.service.tasks.list()})
        self.assertIn(second_task.id, {task.id for task in self.service.tasks.list()})


if __name__ == "__main__":
    unittest.main()
