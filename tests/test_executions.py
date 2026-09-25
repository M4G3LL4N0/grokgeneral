import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.executions import ExecutionRegistry
from grokgeneral.storage import StateStore, canonical_json


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.registry = ExecutionRegistry(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_receipt_lifecycle_keeps_compact_hash_and_redacts(self):
        context = {"task": {"id": "t1"}, "policy": {"api_key": "hidden"}}
        expected_hash = hashlib.sha256(canonical_json({"task": {"id": "t1"}, "policy": {"api_key": "[REDACTED]"}}).encode()).hexdigest()
        receipt = self.registry.start({
            "task_id": "t1",
            "project": "demo",
            "resource": "space-bunny",
            "executor": "Space Bunny",
            "provider": "opencode",
            "model": "opencode/space-bunny-free",
            "context": context,
        })
        self.assertEqual(receipt["status"], "running")
        self.assertEqual(receipt["context_hash"], expected_hash)
        completed = self.registry.complete(receipt["id"], {
            "status": "completed",
            "exit_code": 0,
            "summary": "DONE",
            "artifacts": ["artifact.txt"],
            "validation": {"status": "passed"},
            "usage": {"tokens": 3},
            "raw_events": [{"type": "text", "text": "API_KEY=hidden"}],
        })
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["validation"]["status"], "passed")
        raw = Path(completed["raw_log_path"])
        self.assertTrue(raw.exists())
        self.assertNotIn("hidden", raw.read_text(encoding="utf-8"))
        self.assertNotIn("hidden", json.dumps(self.state.get_record("executions", receipt["id"])))

    def test_list_and_failure_are_filterable(self):
        first = self.registry.start({"task_id": "t1", "project": "a"})
        second = self.registry.start({"task_id": "t2", "project": "b"})
        self.registry.fail(first["id"], "provider failed")
        self.assertEqual(self.registry.get(first["id"])["status"], "failed")
        self.assertEqual(len(self.registry.list(task_id="t1")), 1)
        self.assertEqual(len(self.registry.list(project="b")), 1)
        self.assertEqual(self.registry.get(second["id"])["status"], "running")

    def test_raw_log_path_is_contained_and_receipt_is_small(self):
        receipt = self.registry.start({"task_id": "t1", "context": {"x": 1}})
        completed = self.registry.complete(receipt["id"], {"status": "completed", "raw_events": [{"text": "x" * 10000}]})
        path = Path(completed["raw_log_path"]).resolve()
        self.assertTrue(path.is_relative_to((self.state.state_dir / "execution-logs").resolve()))
        self.assertNotIn("text", completed)
        self.assertLess(len(json.dumps(completed)), 5000)


if __name__ == "__main__":
    unittest.main()
