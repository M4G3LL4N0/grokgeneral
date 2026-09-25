import tempfile
import unittest
from datetime import timedelta

from grokgeneral.doctor import Doctor
from grokgeneral.projects import ProjectRegistry
from grokgeneral.resources import ResourceRegistry
from grokgeneral.storage import StateStore
from grokgeneral.timeutil import isoformat, utc_now


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.projects = ProjectRegistry(self.state)
        self.resources = ResourceRegistry(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_doctor_reports_expired_resource_and_missing_path(self):
        self.projects.add({"id": "missing", "name": "Missing", "path": str(self.temp.name) + "/not-there"})
        self.resources.add({"id": "expired", "name": "expired", "availability": "available", "expires_at": isoformat(utc_now() - timedelta(days=1))})
        diagnostics = Doctor(self.state, self.projects, self.resources).run()
        codes = {item["code"] for item in diagnostics}
        self.assertIn("project.path_missing", codes)
        self.assertIn("resource.expired", codes)
        self.assertTrue(all(item["remediation"] for item in diagnostics if item["severity"] in {"error", "warning"}))

    def test_malformed_registry_data_is_reported_without_crashing(self):
        with self.state.transaction() as connection:
            connection.execute("insert into projects(id,name,path,status,priority,data,created_at,updated_at) values(?,?,?,?,?,?,?,?)", ("broken", "Broken", "/tmp/broken", "unknown", 50, "{bad", "now", "now"))
        diagnostics = Doctor(self.state, self.projects, self.resources).run()
        self.assertIn("state.malformed_data", {item["code"] for item in diagnostics})

    def test_duplicate_paths_are_reported(self):
        first = self.temp.name
        self.projects.add({"id": "one", "name": "One", "path": first})
        record = self.state.get_record("projects", "one")
        with self.state.transaction() as connection:
            self.state.put_record(connection, "projects", {**record, "id": "two", "name": "Two"})
        diagnostics = Doctor(self.state, self.projects, self.resources).run()
        self.assertIn("project.duplicate_path", {item["code"] for item in diagnostics})


if __name__ == "__main__":
    unittest.main()
