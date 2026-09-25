import sys
import tempfile
import unittest
from pathlib import Path

from grokgeneral.adapters import AdapterRegistry, LocalShellAdapter, OpenCodeAdapter
from grokgeneral.errors import ProviderUnavailableError, SafetyBlockedError, ValidationError
from grokgeneral.policies import PolicyEngine
from grokgeneral.storage import StateStore


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.policies = PolicyEngine(self.state)
        self.registry = AdapterRegistry(self.state, self.policies)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_local_shell_runs_explicit_argv_without_shell(self):
        adapter = LocalShellAdapter(self.policies)
        result = adapter.run([sys.executable, "-c", "print('safe')"], cwd=self.temp.name)
        self.assertEqual(result["returncode"], 0)
        self.assertIn("safe", result["stdout"])

    def test_local_shell_rejects_string_and_sensitive_commands_without_approval(self):
        adapter = LocalShellAdapter(self.policies)
        with self.assertRaises(ValidationError):
            adapter.run("echo unsafe")
        with self.assertRaises(SafetyBlockedError):
            adapter.run(["curl", "https://example.com"], approvals=set())
        with self.assertRaises(SafetyBlockedError):
            adapter.run([sys.executable, "-c", "import shutil; shutil.rmtree('/tmp/x')"], approvals=set())

    def test_missing_optional_provider_degrades_to_unavailable(self):
        adapter = OpenCodeAdapter(executable="definitely-not-installed-gg")
        health = adapter.health()
        self.assertFalse(health["available"])
        with self.assertRaises(ProviderUnavailableError):
            adapter.run(["--version"])

    def test_registry_health_lists_optional_adapters(self):
        health = self.registry.health()
        self.assertIn("local-shell", health)
        self.assertIn("opencode", health)
        self.assertTrue(health["local-shell"]["available"])


if __name__ == "__main__":
    unittest.main()
