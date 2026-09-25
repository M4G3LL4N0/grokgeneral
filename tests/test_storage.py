import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from grokgeneral.errors import ValidationError
from grokgeneral.storage import StateStore, atomic_write_json, redact


class StorageTests(unittest.TestCase):
    def test_atomic_json_write_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "value.json"
            value = {"ok": 1, "api_key": "do-not-store", "nested": {"token": "also-secret"}}
            atomic_write_json(path, value)
            self.assertEqual(json.loads(path.read_text())["ok"], 1)
            self.assertEqual(json.loads(path.read_text())["api_key"], "[REDACTED]")
            self.assertEqual(json.loads(path.read_text())["nested"]["token"], "[REDACTED]")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_redact_preserves_non_secret_values(self):
        value = {"count": 3, "flags": ["one", "two"], "secret_name": "not-a-secret-value"}
        result = redact(value)
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["flags"], ["one", "two"])
        self.assertEqual(result["secret_name"], "[REDACTED]")

    def test_redact_preserves_context_token_metrics(self):
        value = {"context_tokens": 100, "max_context_tokens": 200, "token": "secret"}
        result = redact(value)
        self.assertEqual(result["context_tokens"], 100)
        self.assertEqual(result["max_context_tokens"], 200)
        self.assertEqual(result["token"], "[REDACTED]")

    def test_redact_removes_secret_flag_arguments(self):
        result = redact({"command": ["tool", "--token", "secret-value", "--name", "safe"]})
        self.assertEqual(result["command"], ["tool", "--token", "[REDACTED]", "--name", "safe"])

    def test_redact_removes_secret_assignments_inside_text(self):
        result = redact({"output": "API_KEY=top-secret\nTOKEN: another-secret\nordinary text"})
        self.assertNotIn("top-secret", result["output"])
        self.assertNotIn("another-secret", result["output"])
        self.assertIn("ordinary text", result["output"])

    def test_state_initializes_and_persists_records(self):
        with tempfile.TemporaryDirectory() as directory:
            first = StateStore(directory)
            first.initialize()
            with first.transaction() as connection:
                first.put_record(connection, "projects", {
                    "id": "demo",
                    "name": "Demo",
                    "path": "/tmp/demo",
                    "status": "active",
                    "priority": 70,
                    "data": {"tags": ["demo"]},
                })
            first.close()
            second = StateStore(directory)
            record = second.get_record("projects", "demo")
            self.assertEqual(record["name"], "Demo")
            self.assertEqual(record["data"]["tags"], ["demo"])
            self.assertEqual(second.health()["schema_version"], 1)
            second.close()

    def test_transaction_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(directory)
            state.initialize()
            with self.assertRaises(RuntimeError):
                with state.transaction() as connection:
                    state.put_record(connection, "projects", {
                        "id": "rollback",
                        "name": "Rollback",
                        "data": {},
                    })
                    raise RuntimeError("stop")
            self.assertIsNone(state.get_record("projects", "rollback"))
            state.close()

    def test_export_and_import(self):
        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as destination:
            state = StateStore(source)
            state.initialize()
            with state.transaction() as connection:
                state.put_record(connection, "resources", {
                    "id": "free",
                    "name": "free",
                    "status": "available",
                    "data": {"cost_class": "free", "capabilities": ["coding"]},
                })
            snapshot = state.export_snapshot()
            state.close()
            imported = StateStore(destination)
            imported.initialize()
            imported.import_snapshot(snapshot)
            self.assertEqual(imported.get_record("resources", "free")["data"]["cost_class"], "free")
            imported.close()

    def test_concurrent_writers_do_not_lose_committed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(directory)
            state.initialize()
            def write(index):
                worker = StateStore(directory)
                with worker.transaction() as connection:
                    worker.put_record(connection, "projects", {
                        "id": f"p-{index}",
                        "name": f"Project {index}",
                        "data": {"index": index},
                    })
                worker.close()
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(write, range(8)))
            rows = state.list_records("projects")
            self.assertEqual({row["id"] for row in rows}, {f"p-{i}" for i in range(8)})
            state.close()

    def test_part2_control_tables_persist_generic_records(self):
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(directory)
            state.initialize()
            for table in ("approvals", "scheduler_runs", "task_claims"):
                with state.transaction() as connection:
                    state.put_record(connection, table, {"id": f"{table}-1", "status": "pending", "data": {"table": table}})
                self.assertEqual(state.get_record(table, f"{table}-1")["data"]["table"], table)
            state.close()

        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(directory)
            state.initialize()
            with self.assertRaises(ValidationError):
                with state.transaction() as connection:
                    state.put_record(connection, "projects", {"name": "missing id"})
            state.close()


if __name__ == "__main__":
    unittest.main()
