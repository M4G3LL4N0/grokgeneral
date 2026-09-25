import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.cache import Cache
from grokgeneral.errors import ValidationError
from grokgeneral.storage import StateStore, canonical_json


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = StateStore(self.directory.name)
        self.state.initialize()
        self.cache = Cache(self.state)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_key_is_deterministic_and_redacts_before_hashing(self):
        first = self.cache.key("route", {"b": 2, "a": 1, "api_key": "one"})
        second = self.cache.key("route", {"token": "two", "a": 1, "b": 2})

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_put_and_get_persist_only_redacted_values(self):
        key = self.cache.key("result", {"query": "hello"})
        self.cache.put(key, {"answer": "ok", "api_key": "hidden"}, "result", metadata={"authorization": "hidden"})

        value = self.cache.get(key)
        self.assertEqual(value, {"answer": "ok", "api_key": "[REDACTED]"})
        record = self.state.get_record("cache_entries", key)
        self.assertNotIn("hidden", canonical_json(record))
        self.assertEqual(record["metadata"]["authorization"], "[REDACTED]")

    def test_ttl_expiry_removes_entry(self):
        key = self.cache.key("short", {"value": 1})
        self.cache.put(key, {"value": 1}, "short", ttl_seconds=0)

        self.assertIsNone(self.cache.get(key))
        self.assertIsNone(self.state.get_record("cache_entries", key))

    def test_invalidate_by_kind_and_key(self):
        first = self.cache.key("one", {"value": 1})
        second = self.cache.key("one", {"value": 2})
        third = self.cache.key("two", {"value": 3})
        self.cache.put(first, {"value": 1}, "one")
        self.cache.put(second, {"value": 2}, "one")
        self.cache.put(third, {"value": 3}, "two")

        self.assertEqual(self.cache.invalidate(kind="one"), 2)
        self.assertIsNone(self.cache.get(first))
        self.assertIsNone(self.cache.get(second))
        self.assertIsNotNone(self.cache.get(third))
        self.assertEqual(self.cache.invalidate(key=third), 1)

    def test_status_inspect_and_prune_report_entries(self):
        expired = self.cache.key("short", {"value": 1})
        active = self.cache.key("long", {"value": 2})
        self.cache.put(expired, {"value": 1}, "short", ttl_seconds=0)
        self.cache.put(active, {"value": 2}, "long")

        status = self.cache.status()
        inspected = self.cache.inspect()
        self.assertGreaterEqual(status["entries"], 2)
        self.assertTrue(any(item["key"] == active for item in inspected))
        self.assertGreaterEqual(self.cache.prune(), 1)
        self.assertIsNone(self.cache.get(expired))
        self.assertIsNotNone(self.cache.get(active))

    def test_cache_artifacts_and_keys_cannot_escape_cache_directory(self):
        with self.assertRaises(ValidationError):
            self.cache.put("../escape", {"value": 1}, "unsafe")
        with self.assertRaises(ValidationError):
            self.cache.get("../escape")

        key = self.cache.key("safe", {"value": 1})
        self.cache.put(key, {"value": 1}, "safe")
        artifacts = list(self.state.cache_dir.glob("*.json"))
        self.assertTrue(artifacts)
        for artifact in artifacts:
            self.assertEqual(artifact.resolve().parent, self.state.cache_dir.resolve())
            self.assertNotIn("escape", artifact.name)
            self.assertIsInstance(json.loads(artifact.read_text(encoding="utf-8")), dict)


if __name__ == "__main__":
    unittest.main()
