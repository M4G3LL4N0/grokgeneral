import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.service import GrokGeneral
from grokgeneral.storage import canonical_json


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_path = self.root / "repo"
        self.project_path.mkdir()
        (self.project_path / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()
        self.service.projects.add({"id": "repo", "name": "Repo", "path": str(self.project_path), "priority": "core"})
        self.task = self.service.add_task({"id": "task-1", "goal": "review fixture", "project": "repo", "required_capabilities": ["repo-analysis"]})
        self.execution = self.service.executions.start({"task_id": self.task.id, "project": "repo", "resource": "local"})
        self.service.executions.complete(self.execution["id"], {"status": "completed", "summary": "DONE", "validation": {"status": "passed"}, "raw_events": [{"text": "private"}]})

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def assert_compact(self, response):
        self.assertIn("schema_version", response)
        self.assertIn("ok", response)
        self.assertLessEqual(len(canonical_json(response).encode("utf-8")), 8192)
        self.assertNotIn("/Users/", json.dumps(response))
        self.assertNotIn("raw_log_path", json.dumps(response))

    def test_contract_status_and_result_use_compact_envelopes(self):
        status = self.service.contract_status()
        result = self.service.contract_result(execution_id=self.execution["id"])
        self.assertTrue(status["ok"])
        self.assertTrue(result["ok"])
        self.assert_compact(status)
        self.assert_compact(result)
        self.assertEqual(result["result"]["status"], "success")

    def test_contract_execution_requires_approval_id(self):
        response = self.service.contract_request_execution(self.task.id, [], {"allow_execution": True})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "APPROVAL_REQUIRED")
        self.assert_compact(response)

    def test_contract_submit_route_pending_and_changes_are_structured(self):
        submitted = self.service.contract_submit_task({"goal": "run tests", "project": "repo", "required_capabilities": ["testing"]})
        routed = self.service.contract_route_task(submitted["result"]["task_id"])
        pending = self.service.contract_pending_approvals()
        changes = self.service.contract_global_changes(limit=5)
        for response in (submitted, routed, pending, changes):
            self.assert_compact(response)
            self.assertTrue(response["ok"])

    def test_contract_rejects_invalid_payload_without_leaking_exception(self):
        response = self.service.contract_submit_task({"goal": ""})
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_INPUT")
        self.assert_compact(response)
