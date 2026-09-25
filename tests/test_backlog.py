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

    def test_marker_inside_a_string_literal_is_not_work(self):
        (self.project_root / "src" / "tags.ts").write_text('const skip = k !== "fixme";\n', encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        self.assertFalse([item for item in findings if item["kind"] == "todo" and item["path"] == "src/tags.ts"])

    def test_marker_inside_a_regex_literal_is_not_work(self):
        (self.project_root / "src" / "scan.py").write_text('match = re.search(r"\\b(TODO|FIXME|XXX)\\b", text)\n', encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        self.assertFalse([item for item in findings if item["kind"] == "todo" and item["path"] == "src/scan.py"])

    def test_marker_in_a_comment_is_still_reported(self):
        (self.project_root / "src" / "bubble.tsx").write_text("// TODO: Implement retry logic\n", encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        todos = [item for item in findings if item["kind"] == "todo" and item["path"] == "src/bubble.tsx"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["line"], 1)
        self.assertEqual(todos[0]["marker"], "TODO")

    def test_markers_in_tests_fixtures_and_mocks_are_ignored(self):
        tests = self.project_root / "tests"
        tests.mkdir()
        (tests / "test_scan.py").write_text("# TODO: cover the scanner\n", encoding="utf-8")
        (tests / "helper.test.ts").write_text("// TODO: add cases\n", encoding="utf-8")
        mocks = self.project_root / "__mocks__"
        mocks.mkdir()
        (mocks / "data.js").write_text("// TODO: regenerate\n", encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        todo_paths = {item["path"] for item in findings if item["kind"] == "todo"}
        self.assertNotIn("tests/test_scan.py", todo_paths)
        self.assertNotIn("tests/helper.test.ts", todo_paths)
        self.assertNotIn("__mocks__/data.js", todo_paths)

    def test_markers_in_documentation_are_ignored(self):
        (self.project_root / "README.md").write_text("# Report\n\nTODO: rewrite this section\n", encoding="utf-8")
        docs = self.project_root / "docs"
        docs.mkdir()
        (docs / "plan.md").write_text("- TODO: next milestone\n", encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        todo_paths = {item["path"] for item in findings if item["kind"] == "todo"}
        self.assertNotIn("README.md", todo_paths)
        self.assertNotIn("docs/plan.md", todo_paths)
        self.assertEqual(todo_paths, {"src/index.js"})

    def test_markers_in_generated_files_are_ignored(self):
        generated = self.project_root / "src"
        (generated / "api.pb.go").write_text("// TODO: regenerate from proto\n", encoding="utf-8")
        (generated / "schema_pb2.py").write_text("# TODO: regenerate\n", encoding="utf-8")
        (generated / "bundle.min.js").write_text("// TODO: minified\n", encoding="utf-8")
        findings = self.inspector.inspect(self.project)
        todo_paths = {item["path"] for item in findings if item["kind"] == "todo"}
        self.assertNotIn("src/api.pb.go", todo_paths)
        self.assertNotIn("src/schema_pb2.py", todo_paths)
        self.assertNotIn("src/bundle.min.js", todo_paths)

    def test_ignored_markers_are_counted_rather_than_silently_dropped(self):
        (self.project_root / "README.md").write_text("TODO: docs\n", encoding="utf-8")
        tests = self.project_root / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("# TODO: tests\n", encoding="utf-8")
        (self.project_root / "src" / "tags.ts").write_text('const x = "fixme";\n', encoding="utf-8")
        self.inspector.inspect(self.project)
        stats = self.inspector.last_scan_stats
        self.assertEqual(stats["markers_ignored_documentation"], 1)
        self.assertEqual(stats["markers_ignored_tests"], 1)
        self.assertEqual(stats["markers_ignored_literals"], 1)
        self.assertEqual(stats["markers_reported"], 1)

    def test_ignored_markers_remain_available_on_request(self):
        (self.project_root / "README.md").write_text("TODO: docs\n", encoding="utf-8")
        tests = self.project_root / "tests"
        tests.mkdir()
        (tests / "test_a.py").write_text("# TODO: tests\n", encoding="utf-8")
        filtered = {item["path"] for item in self.inspector.inspect(self.project) if item["kind"] == "todo"}
        self.assertEqual(filtered, {"src/index.js"})
        unfiltered = {item["path"] for item in self.inspector.inspect(self.project, include_ignored=True) if item["kind"] == "todo"}
        self.assertEqual(unfiltered, {"src/index.js", "README.md", "tests/test_a.py"})

    def test_findings_carry_deterministic_confidence(self):
        findings = self.inspector.inspect(self.project)
        self.assertTrue(findings)
        for item in findings:
            self.assertIn("confidence", item)
            self.assertIn(item["confidence"], {"high", "medium", "low"})
        todo = next(item for item in findings if item["kind"] == "todo")
        self.assertEqual(todo["confidence"], "medium")
        structure = next(item for item in findings if item["kind"] in {"missing_license", "missing_ci"})
        self.assertEqual(structure["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
