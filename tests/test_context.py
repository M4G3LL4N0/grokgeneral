import json
import tempfile
import unittest
from pathlib import Path

from grokgeneral.cache import Cache
from grokgeneral.context import ContextBuilder
from grokgeneral.errors import ValidationError
from grokgeneral.models import Project, Task
from grokgeneral.storage import StateStore, atomic_write_json


class ProjectRegistry:
    def __init__(self, projects):
        self.projects = projects
        self.requested = []

    def get(self, identifier):
        self.requested.append(identifier)
        return self.projects.get(identifier)


class ContextBuilderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.project_root = self.root / "target"
        self.other_root = self.root / "other"
        self.project_root.mkdir()
        self.other_root.mkdir()
        (self.project_root / "pyproject.toml").write_text('[project]\nname = "target"\n', encoding="utf-8")
        (self.project_root / "safe.md").write_text("# Safe\nVisible content\n", encoding="utf-8")
        (self.project_root / ".env").write_text("API_KEY=hidden\n", encoding="utf-8")
        (self.other_root / "private.md").write_text("OTHER PROJECT SECRET\n", encoding="utf-8")
        self.state = StateStore(self.root / "state")
        self.state.initialize()
        atomic_write_json(self.state.policy_path, {"objective": "safe work", "network": "deny", "api_key": "hidden"})
        self.project = Project(id="target", name="Target", path=str(self.project_root), status="active", tags=["one"])
        self.other = Project(id="other", name="Other", path=str(self.other_root), status="active")
        self.registry = ProjectRegistry({"target": self.project, "other": self.other})
        self.task = Task(
            id="task-1",
            goal="Inspect target",
            project="target",
            context_references=["safe.md"],
        )

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_build_contains_compact_policy_task_project_manifest_and_references(self):
        pack = ContextBuilder(self.state, self.registry).build(self.task, write=False)

        self.assertIsNone(pack.path)
        self.assertEqual(pack.task_id, "task-1")
        self.assertEqual(pack.content["task"]["id"], "task-1")
        self.assertEqual(pack.content["project"]["id"], "target")
        self.assertEqual(pack.content["project"]["status"], "active")
        self.assertIn("target", pack.content["project"]["manifest"])
        self.assertEqual(pack.content["files"][0]["path"], "safe.md")
        self.assertIn("Visible content", pack.content["files"][0]["content"])
        self.assertEqual(pack.content["policy"]["api_key"], "[REDACTED]")
        self.assertNotIn("Other", json.dumps(pack.content))
        self.assertGreater(pack.approx_bytes, 0)

    def test_traversal_and_secret_files_are_rejected(self):
        builder = ContextBuilder(self.state, self.registry)
        for reference in ("../private.md", ".env"):
            task = Task(id="task-2", goal="unsafe", project="target", context_references=[reference])
            with self.assertRaises(ValidationError):
                builder.build(task, write=False)

    def test_write_is_atomic_private_and_contains_redacted_content(self):
        pack = ContextBuilder(self.state, self.registry).build(self.task, write=True)

        self.assertIsNotNone(pack.path)
        path = Path(pack.path)
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("hidden", json.dumps(persisted))
        self.assertFalse(list(path.parent.glob(".*.json")))

    def test_cache_can_reuse_a_context_pack_without_including_unrelated_projects(self):
        cache = Cache(self.state)
        builder = ContextBuilder(self.state, self.registry, cache=cache)
        first = builder.build(self.task, write=False)
        second = builder.build(self.task, write=False)

        self.assertEqual(first.content, second.content)
        self.assertTrue(any(item["kind"] == "context" for item in cache.inspect()))

    def test_task_id_cannot_escape_context_directory(self):
        task = Task(id="../escape", goal="safe", project="target", context_references=["safe.md"])
        pack = ContextBuilder(self.state, self.registry).build(task, write=True)
        self.assertTrue(Path(pack.path).resolve().parent == (self.state.state_dir / "contexts").resolve())

    def test_project_argument_selects_only_that_project(self):
        task = Task(id="task-3", goal="other", project="other", context_references=["private.md"])
        pack = ContextBuilder(self.state, self.registry).build(task, project="target", write=False)

        self.assertEqual(pack.content["project"]["id"], "target")
        self.assertNotIn("OTHER PROJECT SECRET", json.dumps(pack.content))


if __name__ == "__main__":
    unittest.main()
