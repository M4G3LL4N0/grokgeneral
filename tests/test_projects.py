import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.cache import Cache
from grokgeneral.projects import ProjectRegistry
from grokgeneral.storage import StateStore


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "alpha").mkdir()
        (self.root / "alpha" / "README.md").write_text("Alpha", encoding="utf-8")
        (self.root / "alpha" / "package.json").write_text("{}", encoding="utf-8")
        (self.root / ".hidden").mkdir()
        (self.root / ".hidden" / "README.md").write_text("hidden", encoding="utf-8")
        self.state = StateStore(self.root / "state")
        self.state.initialize()
        self.registry = ProjectRegistry(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_scan_discovers_safe_projects_and_preserves_user_fields(self):
        project = self.registry.add({"id": "alpha", "name": "Alpha", "path": str(self.root / "alpha"), "priority": 91, "status": "active"})
        found = self.registry.scan(self.root)
        self.assertEqual(len(found), 1)
        refreshed = self.registry.get("alpha")
        self.assertEqual(refreshed.priority, 91)
        self.assertEqual(refreshed.status, "active")
        self.assertTrue(refreshed.discovered)
        self.assertIsNotNone(self.registry.get("alpha").last_activity)
        self.assertEqual(project.name, "Alpha")

    def test_scan_sanitizes_repository_credentials(self):
        git_dir = self.root / "alpha" / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text('[remote "origin"]\n\turl = https://user:password@example.com/repo.git\n', encoding="utf-8")
        found = self.registry.scan(self.root)
        self.assertEqual(found[0].repository, "https://example.com/repo.git")

    def test_duplicate_paths_are_rejected(self):
        self.registry.add({"id": "one", "name": "One", "path": str(self.root / "alpha")})
        with self.assertRaises(ValueError):
            self.registry.add({"id": "two", "name": "Two", "path": str(self.root / "alpha")})

    def test_update_and_case_insensitive_lookup(self):
        self.registry.add({"id": "alpha", "name": "Alpha", "path": str(self.root / "alpha")})
        self.registry.update("ALPHA", {"priority": 77, "tags": ["one"]})
        result = self.registry.get("alpha")
        self.assertEqual(result.priority, 77)
        self.assertEqual(result.tags, ["one"])

    def test_project_record_does_not_persist_arbitrary_secrets(self):
        self.registry.add({"id": "alpha", "name": "Alpha", "path": str(self.root / "alpha"), "metadata": {"api_key": "secret"}})
        raw = self.state.get_record("projects", "alpha")
        self.assertEqual(raw["data"]["metadata"]["api_key"], "[REDACTED]")

    def test_scan_results_are_cached_without_executing_projects(self):
        registry = ProjectRegistry(self.state, cache=Cache(self.state))
        first = registry.scan(self.root)
        second = registry.scan(self.root)
        self.assertEqual([item.id for item in first], [item.id for item in second])
        self.assertTrue(any(item["kind"] == "project-scan" for item in Cache(self.state).inspect()))

    def test_scan_does_not_execute_project_code(self):
        marker = self.root / "alpha" / "ran"
        (self.root / "alpha" / "setup.py").write_text(f"from pathlib import Path; Path({str(marker)!r}).write_text('bad')", encoding="utf-8")
        self.registry.scan(self.root)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
