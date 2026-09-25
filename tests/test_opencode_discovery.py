import os
import stat
import tempfile
import unittest
from pathlib import Path

from grokgeneral.adapters import AdapterRegistry, OpenCodeAdapter
from grokgeneral.policies import PolicyEngine
from grokgeneral.storage import StateStore


def _fake_binary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/env python3\nprint("9.9.9")\n', encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


class OpenCodeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.state = StateStore(self.root / "state")
        self.state.initialize()
        self.policies = PolicyEngine(self.state)
        self._saved = {key: os.environ.get(key) for key in ("PATH", "HOME", "GG_OPENCODE_BIN")}

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.state.close()
        self.temp.cleanup()

    def _registry(self) -> AdapterRegistry:
        return AdapterRegistry(self.state, self.policies)

    def test_environment_path_wins_over_everything(self):
        configured = _fake_binary(self.root / "configured" / "opencode")
        known = _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ["GG_OPENCODE_BIN"] = str(configured)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(known.parent)
        health = self._registry().get("opencode").health()
        self.assertTrue(health["available"])
        self.assertEqual(health["executable"], str(configured))
        self.assertEqual(health["resolved_via"], "environment")

    def test_path_is_preferred_over_known_local_location(self):
        on_path = _fake_binary(self.root / "bin" / "opencode")
        _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(on_path.parent)
        health = self._registry().get("opencode").health()
        self.assertEqual(health["executable"], str(on_path))
        self.assertEqual(health["resolved_via"], "path")

    def test_known_local_location_is_discovered_without_path(self):
        known = _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.root / "empty")
        health = self._registry().get("opencode").health()
        self.assertTrue(health["available"])
        self.assertEqual(health["executable"], str(known))
        self.assertEqual(health["resolved_via"], "known_location")

    def test_discovered_path_is_persisted_and_reused_on_a_later_run(self):
        known = _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.root / "empty")
        first = self._registry().get("opencode").health()
        self.assertEqual(first["executable"], str(known))
        self.assertEqual(self.state.get_meta("adapters.opencode.executable"), str(known))
        second = self._registry().get("opencode").health()
        self.assertEqual(second["executable"], str(known))
        self.assertEqual(second["resolved_via"], "persisted")

    def test_persisted_path_that_disappeared_is_ignored(self):
        removed = _fake_binary(self.root / "gone" / "opencode")
        self.state.set_meta("adapters.opencode.executable", str(removed))
        removed.unlink()
        _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.root / "empty")
        health = self._registry().get("opencode").health()
        self.assertTrue(health["available"])
        self.assertEqual(health["resolved_via"], "known_location")

    def test_explicitly_configured_path_wins_over_persisted(self):
        stale = _fake_binary(self.root / "stale" / "opencode")
        self.state.set_meta("adapters.opencode.executable", str(stale))
        configured = _fake_binary(self.root / "explicit" / "opencode")
        registry = self._registry()
        registry.set_executable("opencode", str(configured))
        health = registry.get("opencode").health()
        self.assertEqual(health["executable"], str(configured))
        self.assertEqual(health["resolved_via"], "configured")

    def test_configured_path_must_exist(self):
        registry = self._registry()
        with self.assertRaises(Exception):
            registry.set_executable("opencode", str(self.root / "nope" / "opencode"))

    def test_unavailable_reports_search_order(self):
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.root / "empty")
        health = self._registry().get("opencode").health()
        self.assertFalse(health["available"])
        self.assertEqual(health["resolved_via"], "unavailable")
        self.assertTrue(health["searched"])

    def test_discovery_does_not_embed_a_machine_specific_path(self):
        source = Path(__file__).resolve().parents[1] / "grokgeneral" / "adapters.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotIn("/home/", text)

    def test_adapter_uses_resolved_binary_for_runs(self):
        known = _fake_binary(self.home / ".opencode" / "bin" / "opencode")
        os.environ.pop("GG_OPENCODE_BIN", None)
        os.environ["HOME"] = str(self.home)
        os.environ["PATH"] = str(self.root / "empty")
        adapter = self._registry().get("opencode")
        self.assertIsInstance(adapter, OpenCodeAdapter)
        self.assertEqual(adapter._resolved(), str(known))


if __name__ == "__main__":
    unittest.main()
