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

    def test_preexisting_dirty_files_are_not_attributed_to_execution(self):
        receipt = {"id": "execution-4", "task_id": "task-4", "project_id": "concierge", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "summary": "read-only review", "validation": {"status": "passed"}}
        before = {"changes": [" M src/a.py", " M src/b.py", "?? new.txt"]}
        after = {"changes": [" M src/a.py", " M src/b.py", "?? new.txt"]}
        result = compact_result(receipt, before, after)
        self.assertEqual(result["files_changed"], 0)
        self.assertTrue(result["preexisting_dirty"])
        self.assertEqual(result["files_changed_by_execution"], [])
        self.assertEqual(result["files_added_by_execution"], [])
        self.assertEqual(result["files_deleted_by_execution"], [])

    def test_only_new_changes_are_attributed_to_execution(self):
        receipt = {"id": "execution-5", "task_id": "task-5", "project_id": "concierge", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "summary": "fixed it", "validation": {"status": "passed"}}
        before = {"changes": [" M src/a.py", " M src/b.py"]}
        after = {"changes": [" M src/a.py", " M src/b.py", " M src/c.py", "?? added.py", "?? scratch.txt"]}
        result = compact_result(receipt, before, after)
        self.assertTrue(result["preexisting_dirty"])
        self.assertEqual(result["files_changed"], 3)
        self.assertEqual(sorted(result["files_changed_by_execution"]), ["added.py", "scratch.txt", "src/c.py"])
        self.assertEqual(result["files_added_by_execution"], ["added.py", "scratch.txt"])
        self.assertEqual(result["files_deleted_by_execution"], [])

    def test_deleted_by_execution_is_reported(self):
        receipt = {"id": "execution-6", "task_id": "task-6", "project_id": "concierge", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "summary": "removed dead code", "validation": {"status": "passed"}}
        before = {"changes": [" M src/a.py", "?? gone.py"]}
        after = {"changes": [" M src/a.py"]}
        result = compact_result(receipt, before, after)
        self.assertEqual(result["files_changed"], 1)
        self.assertEqual(result["files_deleted_by_execution"], ["gone.py"])
        self.assertEqual(result["files_added_by_execution"], [])

    def test_uncertain_attribution_is_reported_as_unknown(self):
        receipt = {"id": "execution-7", "task_id": "task-7", "project_id": "concierge", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "summary": "no snapshot", "validation": {"status": "not-run"}}
        result = compact_result(receipt, None, None)
        self.assertIsNone(result["files_changed"])
        self.assertEqual(result["files_changed_by_execution"], None)
        self.assertIsNone(result["preexisting_dirty"])
        self.assertIn("file attribution unavailable", " ".join(result["warnings"]))

    def test_rename_is_not_counted_as_a_brand_new_file(self):
        receipt = {"id": "execution-8", "task_id": "task-8", "project_id": "concierge", "provider": "opencode", "model": "opencode/space-bunny-free", "executor": "Space Bunny", "status": "completed", "summary": "renamed", "validation": {"status": "passed"}}
        before = {"changes": []}
        after = {"changes": ["R  old.py -> new.py"]}
        result = compact_result(receipt, before, after)
        self.assertEqual(result["files_added_by_execution"], [])
        self.assertEqual(result["files_deleted_by_execution"], [])
        self.assertIn("new.py", result["files_changed_by_execution"])
        self.assertEqual(result["files_changed"], 1)
