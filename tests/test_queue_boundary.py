import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from grokgeneral.errors import ValidationError
from grokgeneral.scheduler import Scheduler, SchedulerConfig
from grokgeneral.service import GrokGeneral
from grokgeneral.timeutil import isoformat, parse_time, utc_now


class QueueMigrationBoundaryTests(unittest.TestCase):
    """The shared queue owns work; these tests pin the primitives external
    queues would need before migrating, so migration reuses this scheduler
    instead of introducing a second one."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.service = GrokGeneral(self.root / "state")
        self.service.initialize()
        self.service.projects.add({"id": "repo", "name": "Repo", "path": str(self.repo), "priority": "core"})
        self.scheduler = self.service.scheduler
        self.calls = []

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def add_task(self, identifier):
        return self.service.tasks.add({"id": identifier, "goal": identifier, "project": "repo", "status": "queued", "required_capabilities": ["coding"], "metadata": {"command": ["python3", "-c", "print('ok')"]}})

    def failing_dispatcher(self, task, approval_ids):
        self.calls.append(task.id)
        return {"status": "failed", "task_id": task.id, "error": "boom"}

    def test_cooldown_defaults_to_disabled_so_existing_behaviour_is_unchanged(self):
        config = SchedulerConfig()
        self.assertEqual(config.cooldown_seconds, 0)
        self.assertEqual(config.validate().cooldown_seconds, 0)

    def test_cooldown_must_be_a_non_negative_number(self):
        for value in (-1, "30", True, None):
            with self.assertRaises(ValidationError):
                SchedulerConfig(cooldown_seconds=value).validate()

    def test_failed_task_is_not_retried_during_its_cooldown(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        self.add_task("flaky")
        first = self.scheduler.run_cycle(execute=True, config=SchedulerConfig(retry_failed=True, cooldown_seconds=600))
        self.assertEqual(first["failed"], 1)
        self.assertEqual(self.calls, ["flaky"])
        second = self.scheduler.run_cycle(execute=True, config=SchedulerConfig(retry_failed=True, cooldown_seconds=600))
        self.assertEqual(second["selected"], 0)
        self.assertEqual(self.calls, ["flaky"])
        self.assertEqual(second["cooldown_skipped"], 1)

    def test_task_is_retried_once_the_cooldown_expires(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        task = self.add_task("flaky")
        self.scheduler.run_cycle(execute=True, config=SchedulerConfig(retry_failed=True, cooldown_seconds=600))
        self.assertEqual(self.service.tasks.get(task.id).status, "queued")
        current = self.service.tasks.get(task.id)
        with self.service.state.transaction() as connection:
            self.service.state.put_record(connection, "tasks", {**current.to_record(), "data": {**(current.metadata or {}), "cooldown_until": isoformat(utc_now() - timedelta(seconds=1))}})
        again = self.scheduler.run_cycle(execute=True, config=SchedulerConfig(retry_failed=True, cooldown_seconds=600))
        self.assertEqual(again["selected"], 1)
        self.assertEqual(self.calls, ["flaky", "flaky"])

    def test_cooldown_uses_the_recorded_failure_time(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        task = self.add_task("flaky")
        self.scheduler.run_cycle(execute=True, config=SchedulerConfig(retry_failed=True, cooldown_seconds=600))
        current = self.service.tasks.get(task.id)
        self.assertIn("cooldown_until", current.metadata)
        self.assertGreater(parse_time(current.metadata["cooldown_until"]), utc_now())

    def test_claim_records_owner_and_lease(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        self.add_task("owned")
        self.scheduler.run_cycle(execute=True)
        claims = [item for item in self.service.state.list_records("task_claims") if item.get("task_id") == "owned"]
        self.assertEqual(len(claims), 1)
        data = claims[0]["data"]
        self.assertEqual(data["owner"], "scheduler")
        self.assertTrue(data["owner_id"])
        self.assertTrue(data["lease_expires_at"])

    def test_expired_lease_frees_the_task_for_another_claim(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        self.add_task("leased")
        entry = next(item for item in self.scheduler.plan() if item["task_id"] == "leased")
        claim, _reason = self.scheduler._claim(entry, "run-1", SchedulerConfig())
        self.assertIsNotNone(claim)
        _again, blocked = self.scheduler._claim(entry, "run-2", SchedulerConfig())
        self.assertEqual(blocked, "already_claimed")
        record = self.service.state.get_record("task_claims", claim["id"])
        data = {**record["data"], "lease_expires_at": isoformat(utc_now() - timedelta(seconds=5))}
        with self.service.state.transaction() as connection:
            self.service.state.put_record(connection, "task_claims", {**record, "data": data})
        _third, allowed = self.scheduler._claim(entry, "run-3", SchedulerConfig())
        self.assertIsNone(allowed)

    def test_priority_and_dependencies_still_gate_claims(self):
        first = self.service.tasks.add({"id": "dep", "goal": "dep", "project": "repo", "status": "queued"})
        second = self.service.tasks.add({"id": "dependent", "goal": "dependent", "project": "repo", "status": "queued", "dependencies": [first.id]})
        entries = {item["task_id"]: item for item in self.scheduler.plan()}
        self.assertEqual(entries["dependent"].get("blocked"), "dependencies are not complete")
        self.assertNotIn("blocked", entries["dep"])

    def test_second_scheduler_cannot_claim_the_same_task(self):
        self.scheduler.dispatcher = self.failing_dispatcher
        self.add_task("single-owner")
        other = Scheduler(self.service.state, self.service.tasks, self.service.router, service=self.service)
        entry = next(item for item in self.scheduler.plan() if item["task_id"] == "single-owner")
        first, _ = self.scheduler._claim(entry, "run-a", SchedulerConfig())
        self.assertIsNotNone(first)
        second, reason = other._claim(entry, "run-b", SchedulerConfig())
        self.assertIsNone(second)
        self.assertEqual(reason, "already_claimed")


if __name__ == "__main__":
    unittest.main()
