import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from grokgeneral.errors import SafetyBlockedError, ValidationError
from grokgeneral.policies import PolicyEngine
from grokgeneral.storage import StateStore
from grokgeneral.validation import PermissionPolicy, RepositorySnapshot, ValidationRunner


class ExecutionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test"], check=True)
        (self.repo / "README.md").write_text("clean", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.state = StateStore(self.root / "state")
        self.state.initialize()
        self.policies = PolicyEngine(self.state)
        self.runner = ValidationRunner(self.policies)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_snapshot_records_branch_head_and_dirty_changes_without_cleanup(self):
        clean = RepositorySnapshot.capture(self.repo)
        self.assertFalse(clean["dirty"])
        self.assertTrue(clean["head"])
        (self.repo / "README.md").write_text("dirty", encoding="utf-8")
        dirty = RepositorySnapshot.capture(self.repo)
        self.assertTrue(dirty["dirty"])
        self.assertTrue(dirty["changes"])
        self.assertEqual((self.repo / "README.md").read_text(encoding="utf-8"), "dirty")

    def test_validation_accepts_only_argv_arrays_and_passes(self):
        result = self.runner.run(self.repo, [["python3", "-c", "print('ok')"]], approvals={"validate"})
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["exit_code"], 0)
        with self.assertRaises(ValidationError):
            self.runner.run(self.repo, ["python3 -c pass"], approvals={"validate"})

    def test_validation_detects_changes_without_reverting(self):
        marker = self.repo / "generated.txt"
        result = self.runner.run(self.repo, [["python3", "-c", f"open({str(marker)!r}, 'w').write('x')"]], approvals={"validate"})
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["changed"])
        self.assertTrue(marker.exists())

    def test_permissions_are_separate(self):
        permission = PermissionPolicy(self.policies)
        self.assertTrue(permission.authorize("inspect", set()).allowed)
        self.assertFalse(permission.authorize("validate", set()).allowed)
        self.assertTrue(permission.authorize("validate", {"validate"}).allowed)
        self.assertFalse(permission.authorize("modify", set()).allowed)
        self.assertFalse(permission.authorize("push", set()).allowed)
        self.assertTrue(permission.authorize("push", {"push"}).allowed)
        with self.assertRaises(SafetyBlockedError):
            permission.require("commit", set())


if __name__ == "__main__":
    unittest.main()
