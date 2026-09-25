import unittest

from grokgeneral.models import Project, Resource, Task
from grokgeneral.timeutil import parse_duration, parse_time


class ModelTests(unittest.TestCase):
    def test_duration_parser_supports_compact_and_long_forms(self):
        self.assertEqual(parse_duration("7d").days, 7)
        self.assertEqual(parse_duration("2 hours").total_seconds(), 7200)
        self.assertEqual(parse_duration("30m").total_seconds(), 1800)

    def test_time_parser_normalizes_to_utc(self):
        parsed = parse_time("2026-09-24T12:00:00-04:00")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.isoformat(), "2026-09-24T16:00:00+00:00")

    def test_models_round_trip_through_dict(self):
        project = Project(id="p", name="Project", path="/tmp/p", tags=["one"])
        self.assertEqual(Project.from_dict(project.to_dict()).to_dict(), project.to_dict())
        resource = Resource(id="r", name="r", provider="local", capabilities=["coding"])
        self.assertEqual(Resource.from_dict(resource.to_dict()).to_dict(), resource.to_dict())
        task = Task(id="t", goal="test", required_capabilities=["testing"])
        self.assertEqual(Task.from_dict(task.to_dict()).to_dict(), task.to_dict())


if __name__ == "__main__":
    unittest.main()
