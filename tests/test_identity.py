import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from grokgeneral.cli import main
from grokgeneral.models import Project
from grokgeneral.projects import ProjectRegistry, RootRegistry
from grokgeneral.service import GrokGeneral
from grokgeneral.storage import StateStore


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "startups"
        self.root.mkdir()
        self.project_path = self.root / "gh0st"
        self.project_path.mkdir()
        (self.project_path / "package.json").write_text("{}", encoding="utf-8")
        self.state = StateStore(Path(self.temp.name) / "state")
        self.state.initialize()
        self.projects = ProjectRegistry(self.state)
        self.roots = RootRegistry(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_project_identity_fields_and_alias_lookup(self):
        project = self.projects.add({
            "id": "gh0st",
            "name": "gh0st",
            "path": str(self.project_path),
            "canonical_id": "github-ghost",
            "aliases": ["Ghost CLI"],
            "kind": "tool",
            "priority": "core",
        })
        self.assertEqual(project.canonical_id, "github-ghost")
        self.assertEqual(self.projects.get("Ghost CLI").id, "gh0st")
        self.assertEqual(self.projects.get("github-ghost").id, "gh0st")
        self.assertEqual(self.projects.get("gh0st").priority, "core")

    def test_roots_are_explicit_and_deduplicated(self):
        root = self.roots.add(self.root)
        same = self.roots.add(self.root)
        self.assertEqual(root["id"], same["id"])
        self.assertEqual(self.roots.list()[0]["path"], str(self.root.resolve()))
        self.assertTrue(self.roots.remove(root["id"]))
        self.assertEqual(self.roots.list(), [])

    def test_manual_identity_survives_rescan(self):
        self.projects.add({"id": "gh0st", "name": "gh0st", "path": str(self.project_path), "kind": "tool", "priority": "core", "aliases": ["ghost"]})
        self.projects.scan(self.root)
        refreshed = self.projects.get("gh0st")
        self.assertEqual(refreshed.kind, "tool")
        self.assertEqual(refreshed.priority, "core")
        self.assertIn("ghost", refreshed.aliases)

    def test_project_priority_filter_and_cli_identity_commands(self):
        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "--state-dir", str(self.state.state_dir),
                "root", "add", str(self.root), "--json",
            ])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertEqual(json.loads(output.getvalue())["path"], str(self.root.resolve()))

        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "--state-dir", str(self.state.state_dir),
                "project", "add", "--id", "gh0st", "--name", "gh0st", "--path", str(self.project_path), "--json",
            ])
        self.assertEqual(code, 0, errors.getvalue())

        for args in (
            ["project", "alias", "add", "gh0st", "ghost", "--json"],
            ["project", "priority", "gh0st", "core", "--json"],
        ):
            output = io.StringIO()
            errors = io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["--state-dir", str(self.state.state_dir), *args])
            self.assertEqual(code, 0, errors.getvalue())

        output = io.StringIO()
        errors = io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["--state-dir", str(self.state.state_dir), "projects", "--priority", "core", "--json"])
        self.assertEqual(code, 0, errors.getvalue())
        values = json.loads(output.getvalue())
        self.assertEqual([item["id"] for item in values], ["gh0st"])

    def test_service_scans_only_configured_roots(self):
        service = GrokGeneral(self.state.state_dir)
        service.roots.add(self.root)
        try:
            found = service.scan_projects()
            self.assertEqual([item.id for item in found], ["gh0st"])
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
