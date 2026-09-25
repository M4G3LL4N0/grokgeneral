import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from grokgeneral.errors import SafetyBlockedError
from grokgeneral.service import GrokGeneral


class FakeOpenCode:
    name = "opencode"

    def __init__(self, status="completed", error=None, mutate_path=None):
        self.status = status
        self.error = error
        self.mutate_path = mutate_path
        self.calls = []

    def run(self, request, **kwargs):
        self.calls.append({"request": request, **kwargs})
        if self.error is not None:
            raise self.error
        if self.mutate_path is not None:
            self.mutate_path.write_text("changed by provider", encoding="utf-8")
        if self.status == "failed":
            return {"status": "failed", "exit_code": 1, "error": "failed", "events": [], "summary": "", "usage": None}
        return {"status": "completed", "exit_code": 0, "error": None, "events": [{"type": "text", "text": "DONE"}], "summary": "DONE", "usage": {"total": 3, "cost": 0}}


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "README.md").write_text("project", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()
        self.service.projects.add({"id": "repo", "name": "Repo", "path": str(self.repo), "kind": "tool", "priority": "core"})
        self.service.set_project_validation("repo", [["python3", "-c", "print('validated')"]])
        self.task = self.service.add_task({"goal": "read-only repository review", "project": "repo", "required_capabilities": ["repo-analysis"]})
        self.fake = FakeOpenCode()
        self.service.adapters.adapters["opencode"] = self.fake

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_real_pipeline_creates_receipt_and_completes_only_after_validation(self):
        result = self.service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
        self.assertEqual(result["task"]["status"], "completed")
        self.assertEqual(result["receipt"]["validation"]["status"], "passed")
        self.assertEqual(len(self.service.executions_list(task_id=self.task.id)), 1)
        self.assertEqual(self.fake.calls[0]["allow_execution"], True)
        self.assertIn("DONE", json.dumps(result))

    def test_dry_run_does_not_create_receipt_or_change_task(self):
        result = self.service.executor_run(self.task.id, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.service.tasks.get(self.task.id).status, "proposed")
        self.assertEqual(self.service.executions_list(task_id=self.task.id), [])

    def test_zero_exit_without_validation_is_not_success(self):
        self.service.set_project_validation("repo", [["python3", "-c", "raise SystemExit(1)"]])
        result = self.service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
        self.assertEqual(result["task"]["status"], "failed")
        self.assertEqual(result["receipt"]["status"], "failed")
        self.assertEqual(result["receipt"]["validation"]["status"], "failed")
        self.assertIn("did not pass", result["receipt"]["error"])

    def test_missing_validation_configuration_is_reported_distinctly_from_failure(self):
        record = self.service.projects.get("repo").to_record()
        metadata = dict((record.get("data") or {}).get("metadata") or {})
        metadata.pop("validation_commands", None)
        with self.service.state.transaction() as connection:
            self.service.state.put_record(connection, "projects", {**record, "data": {**(record.get("data") or {}), "metadata": metadata}})
        result = self.service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
        self.assertEqual(result["receipt"]["status"], "failed")
        self.assertEqual(result["receipt"]["validation"]["status"], "pending")
        self.assertIn("not configured", result["receipt"]["error"])
        self.assertNotIn("did not pass", result["receipt"]["error"])

    def test_provider_exception_finishes_receipt_and_task(self):
        self.fake.error = RuntimeError("provider crashed")
        result = self.service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
        self.assertEqual(result["receipt"]["status"], "failed")
        self.assertEqual(result["task"]["status"], "failed")
        self.assertIn("provider crashed", result["receipt"]["error"])

    def test_unexpected_provider_changes_are_recorded_without_revert(self):
        self.fake.mutate_path = self.repo / "README.md"
        result = self.service.executor_run(self.task.id, allow_execution=True, approvals={"validate"})
        self.assertEqual(result["receipt"]["status"], "failed")
        self.assertTrue(result["receipt"]["snapshot"]["changed"])
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "changed by provider")

    def test_execution_requires_explicit_allow_execution(self):
        with self.assertRaises(Exception):
            self.service.executor_run(self.task.id)

    def test_validation_requires_explicit_validation_approval(self):
        with self.assertRaises(SafetyBlockedError):
            self.service.executor_run(self.task.id, allow_execution=True)

    def test_durable_validation_approval_can_be_consumed_by_executor(self):
        approval = self.service.request_approval(self.task.id, ["validate"])
        self.service.approval_approve(approval["id"])
        result = self.service.executor_run(self.task.id, allow_execution=True, approval_ids=[approval["id"]])
        self.assertEqual(result["task"]["status"], "completed")
        self.assertEqual(self.service.approval_show(approval["id"])["status"], "consumed")


if __name__ == "__main__":
    unittest.main()
