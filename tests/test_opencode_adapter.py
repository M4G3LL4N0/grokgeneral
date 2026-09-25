import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from grokgeneral.adapters import OpenCodeAdapter
from grokgeneral.errors import ProviderUnavailableError, SafetyBlockedError, ValidationError


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.log = self.root / "argv.json"
        self.executable = self.root / "fake-opencode"
        script = f'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
log = Path({str(self.log)!r})
log.write_text(json.dumps(sys.argv[1:]))
if "--version" in sys.argv:
    print("fake-version")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "models":
    print("opencode/space-bunny-free")
    print("opencode/other-model")
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "run":
    if "--model" in sys.argv and sys.argv[sys.argv.index("--model") + 1].endswith("not-real"):
        print(json.dumps({{"type":"error","error":{{"name":"UnknownError","data":{{"message":"bad model"}}}}}}))
        raise SystemExit(1)
    if any("sleep" in item for item in sys.argv):
        time.sleep(2)
    print(json.dumps({{"type":"step_start","sessionID":"session-1","timestamp":1}}))
    print(json.dumps({{"type":"text","sessionID":"session-1","part":{{"type":"text","text":"DONE"}}}}))
    print(json.dumps({{"type":"step_finish","sessionID":"session-1","part":{{"type":"step-finish","tokens":{{"total":4,"input":3,"output":1}},"cost":0}}}}))
    raise SystemExit(0)
if len(sys.argv) > 1 and sys.argv[1] == "invalid":
    print(json.dumps({{"type":"error","error":{{"name":"UnknownError","data":{{"message":"bad model"}}}}}}))
    raise SystemExit(1)
raise SystemExit(2)
'''
        self.executable.write_text(script, encoding="utf-8")
        self.executable.chmod(self.executable.stat().st_mode | stat.S_IXUSR)
        self.adapter = OpenCodeAdapter(executable=str(self.executable), timeout=5)
        self.project = self.root / "project"
        self.project.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_health_and_models_use_documented_commands(self):
        health = self.adapter.health()
        models = self.adapter.models(provider="opencode")
        self.assertTrue(health["available"])
        self.assertEqual(health["version"], "fake-version")
        self.assertEqual([item["id"] for item in models], ["opencode/space-bunny-free", "opencode/other-model"])
        self.assertEqual(json.loads(self.log.read_text())[:3], ["models", "opencode"])

    def test_run_uses_exact_safe_argv_and_parses_jsonl(self):
        result = self.adapter.run("Inspect the project", cwd=self.project, model="opencode/space-bunny-free", allow_execution=True)
        argv = json.loads(self.log.read_text())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["summary"], "DONE")
        self.assertNotIn("--auto", argv)
        self.assertEqual(argv[:2], ["run", "--pure"])
        self.assertIn("--format", argv)
        self.assertIn("--dir", argv)
        self.assertEqual(argv[argv.index("--dir") + 1], str(self.project.resolve()))
        self.assertEqual(argv[argv.index("--model") + 1], "opencode/space-bunny-free")
        self.assertEqual(argv[-1], "Inspect the project")

    def test_dry_run_does_not_execute(self):
        result = self.adapter.run("do not execute", cwd=self.project, model="opencode/space-bunny-free", dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.log.exists())
        self.assertIn("--format", result["command"])

    def test_timeout_and_invalid_model_are_reported(self):
        timeout = self.adapter.run("sleep", cwd=self.project, model="opencode/space-bunny-free", timeout=0.01, allow_execution=True)
        self.assertEqual(timeout["status"], "timeout")
        invalid = self.adapter.run("bad", cwd=self.project, model="opencode/not-real", allow_execution=True)
        self.assertEqual(invalid["status"], "failed")
        self.assertEqual(invalid["exit_code"], 1)
        self.assertIn("bad model", invalid["error"])

    def test_explicit_execution_and_valid_cwd_are_required(self):
        with self.assertRaises(SafetyBlockedError):
            self.adapter.run("hello", cwd=self.project, model="opencode/space-bunny-free")
        with self.assertRaises(ValidationError):
            self.adapter.run("hello", cwd=self.root / "missing", model="opencode/space-bunny-free", allow_execution=True)
        with self.assertRaises(ValidationError):
            self.adapter.run("", cwd=self.project, model="opencode/space-bunny-free", allow_execution=True)


if __name__ == "__main__":
    unittest.main()
