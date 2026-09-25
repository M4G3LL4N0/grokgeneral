import json
import unittest

from grokgeneral.results import compact_result
from grokgeneral.storage import canonical_json


class ResultTests(unittest.TestCase):
    def test_compact_result_has_stable_shape_and_no_raw_paths(self):
        receipt = {
            "id": "execution-1",
            "task_id": "task-1",
            "project_id": "gh0st",
            "executor": "Space Bunny",
            "provider": "opencode",
            "model": "opencode/space-bunny-free",
            "status": "completed",
            "summary": "DONE " + "x" * 5000,
            "validation": {"status": "passed", "exit_code": 0},
            "usage": {"total": 4},
            "raw_log_path": "/Users/private/execution.json",
            "error": None,
        }
        before = {"changes": []}
        after = {"changes": [" M src/app.py", " M tests/test_app.py"]}
        task = {"project": "gh0st", "metadata": {"blockers": []}}
        result = compact_result(receipt, before, after, task)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["project"], "gh0st")
        self.assertEqual(result["files_changed"], 2)
        self.assertEqual(result["tests"], "passed")
        self.assertIsNone(result["commit"])
        self.assertNotIn("raw_log_path", result)
        self.assertNotIn("/Users/", json.dumps(result))
        self.assertLessEqual(len(canonical_json(result)), 16384)

    def test_failed_validation_is_not_success(self):
        receipt = {"id": "execution-2", "project_id": "gh0st", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "validation": {"status": "failed", "exit_code": 1}, "summary": "", "error": "validation failed"}
        result = compact_result(receipt, {"changes": []}, {"changes": []})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["tests"], "failed")
        self.assertEqual(result["next_action"], "review validation failure")

    def test_large_artifact_metadata_is_bounded(self):
        receipt = {"id": "execution-3", "project_id": "gh0st", "executor": "local", "status": "completed", "summary": "ok", "artifacts": [{"path": "/Users/private/file", "data": "x" * 10000}], "validation": {"status": "passed"}}
        result = compact_result(receipt, {"changes": []}, {"changes": []})
        self.assertLessEqual(len(canonical_json(result)), 16384)
        self.assertNotIn("/Users/", json.dumps(result))
