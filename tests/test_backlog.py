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
        self.assertEqual(sorted(item.name for item in self.project_root.iterdir()), before)

    def test_proposals_are_data_only(self):
        proposals = self.inspector.propose(self.inspector.inspect(self.project))
        self.assertTrue(proposals)
        self.assertTrue(all("goal" in item and "id" in item for item in proposals))
        self.assertEqual(len(list(self.project_root.iterdir())), 3)


if __name__ == "__main__":
    unittest.main()
