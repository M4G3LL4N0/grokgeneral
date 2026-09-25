import tempfile
import unittest
from pathlib import Path

from grokgeneral.backlog import BacklogInspector
from grokgeneral.projects import ProjectRegistry
from grokgeneral.storage import StateStore


class BacklogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project_root = self.root / "repo"
        self.project_root.mkdir()
        (self.project_root / "package.json").write_text("{}", encoding="utf-8")
        (self.project_root / "README.md").write_text("old docs", encoding="utf-8")
        (self.project_root / "src").mkdir()
        (self.project_root / "src" / "index.js").write_text("// TODO: finish\n", encoding="utf-8")
        self.state = StateStore(self.root / "state")
        self.state.initialize()
        self.projects = ProjectRegistry(self.state)
        self.project = self.projects.add({"id": "repo", "name": "Repo", "path": str(self.project_root)})
        self.inspector = BacklogInspector(self.state, self.projects)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_inspection_returns_evidence_without_modifying_repo(self):
        before = sorted(item.name for item in self.project_root.iterdir())
        findings = self.inspector.inspect(self.project)
        self.assertTrue(findings)
        self.assertTrue(any(item["kind"] == "todo" for item in findings))
        todo = next(item for item in findings if item["kind"] == "todo")
        self.assertEqual(todo["path"], "src/index.js")
        self.assertEqual(todo["line"], 1)
        self.assertEqual(todo["marker"], "TODO")
        self.assertTrue(todo["fingerprint"])
        self.assertEqual(sorted(item.name for item in self.project_root.iterdir()), before)

    def test_proposals_are_data_only(self):
        proposals = self.inspector.propose(self.inspector.inspect(self.project))
        self.assertTrue(proposals)
        self.assertTrue(all("goal" in item and "id" in item for item in proposals))
        self.assertEqual(len(list(self.project_root.iterdir())), 3)

    def test_walk_prunes_excluded_directories(self):
        for excluded in ("node_modules", "dist", "build", "vendor", ".git", "__pycache__"):
            directory = self.project_root / excluded / "nested"
            directory.mkdir(parents=True)
            (directory / "index.js").write_text("// TODO: vendored\n", encoding="utf-8")
        files, _stats = self.inspector._walk_files(self.project_root, limit=200, max_entries=1000)
        relative = {str(path.relative_to(self.project_root)) for path in files}
        self.assertIn("src/index.js", relative)
        for excluded in ("node_modules", "dist", "build", "vendor", ".git", "__pycache__"):
            self.assertFalse([name for name in relative if excluded in name], relative)

    def test_walk_bounds_visited_entries(self):
        heavy = self.project_root / "src" / "deep"
        heavy.mkdir(parents=True)
        for index in range(400):
            (heavy / f"file{index}.js").write_text("x", encoding="utf-8")
        files, stats = self.inspector._walk_files(self.project_root, limit=200, max_entries=25)
        self.assertLessEqual(stats["entries_visited"], 25)
        self.assertTrue(stats["truncated"])
        self.assertLessEqual(len(files), 25)

    def test_walk_stops_at_requested_file_limit(self):
        source = self.project_root / "src"
        for index in range(40):
            (source / f"mod{index}.js").write_text("x", encoding="utf-8")
        files, stats = self.inspector._walk_files(self.project_root, limit=5, max_entries=1000)
        self.assertEqual(len(files), 5)
        self.assertTrue(stats["truncated"])

    def test_inspect_reports_scan_stats_within_bounds(self):
        heavy = self.project_root / "node_modules"
        heavy.mkdir()
        for index in range(300):
            (heavy / f"mod{index}.js").write_text("// TODO: vendored\n", encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        stats = self.inspector.last_scan_stats
        self.assertTrue(any(item["kind"] == "todo" for item in findings))
        self.assertLessEqual(stats["entries_visited"], self.inspector.MAX_ENTRIES)
        self.assertGreaterEqual(stats["pruned_dirs"], 1)
        self.assertFalse(any("node_modules" in (item["path"] or "") for item in findings))
        self.assertEqual(stats["files_scanned"], 3)


if __name__ == "__main__":
    unittest.main()
