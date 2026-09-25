import tempfile
import threading
import time
import unittest
from pathlib import Path

from grokgeneral.scheduler import SchedulerConfig
from grokgeneral.service import GrokGeneral


class SchedulerConcurrencyTests(unittest.TestCase):
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
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def add_task(self, identifier, modify=False):
        return self.service.tasks.add({"id": identifier, "goal": identifier, "project": "repo", "status": "queued", "required_capabilities": ["coding"], "metadata": {"command": ["python3", "-c", "print('ok')"], "modify": modify}})

    def dispatcher(self, task, approval_ids):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(task.id)
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return {"status": "completed", "task_id": task.id, "execution_id": None}

    def test_default_cycle_is_bounded_to_two_workers(self):
        self.scheduler.dispatcher = self.dispatcher
        for index in range(5):
            self.add_task(f"task-{index}")
        result = self.scheduler.run_cycle(execute=True)
        self.assertLessEqual(result["concurrency"], 2)
        self.assertLessEqual(result["selected"], 4)
        self.assertLessEqual(self.max_active, 2)

    def test_mutation_claims_for_one_repository_are_serialized(self):
        self.scheduler.dispatcher = self.dispatcher
        first = self.add_task("mutation-one", modify=True)
        second = self.add_task("mutation-two", modify=True)
        approvals = []
        for task in (first, second):
            approval = self.service.request_approval(task.id, ["modify"])
            self.service.approval_approve(approval["id"])
            approvals.append(approval["id"])
        result = self.scheduler.run_cycle(execute=True, approval_ids=approvals)
        self.assertEqual(result["same_repo_conflicts"], 1)
        self.assertEqual(self.max_active, 1)

    def test_read_only_claims_for_one_repository_can_run_together(self):
        self.scheduler.dispatcher = self.dispatcher
        self.add_task("read-one")
        self.add_task("read-two")
        result = self.scheduler.run_cycle(execute=True)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(self.max_active, 2)

    def test_pause_stops_new_claims(self):
        self.scheduler.pause()
        self.add_task("paused-task")
        result = self.scheduler.run_cycle(execute=True)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["selected"], 0)

    def test_two_schedulers_cannot_claim_the_same_task(self):
        self.scheduler.dispatcher = self.dispatcher
        self.add_task("single-task")
        other = type(self.scheduler)(self.service.state, self.service.tasks, self.service.router, service=self.service)
        other.dispatcher = self.dispatcher
        first = self.scheduler.run_cycle(execute=True, config=SchedulerConfig(concurrency=1, max_tasks=1, max_seconds=10, max_attempts=1))
        second = other.run_cycle(execute=True, config=SchedulerConfig(concurrency=1, max_tasks=1, max_seconds=10, max_attempts=1))
        self.assertEqual(first["selected"], 1)
        self.assertEqual(second["selected"], 0)

    def test_scheduler_scopes_approval_ids_to_each_task(self):
        self.service.set_project_validation("repo", [["python3", "-c", "print('ok')"]])
        first = self.add_task("approved-one")
        second = self.add_task("approved-two")
        approval_ids = []
        for task in (first, second):
            approval = self.service.request_approval(task.id, ["validate"])
            self.service.approval_approve(approval["id"])
            approval_ids.append(approval["id"])

        def consume(task, ids):
            self.service.approvals.consume(ids, task, 1, {"validate"})
            return {"status": "completed", "task_id": task.id}

        self.scheduler.dispatcher = consume
        result = self.scheduler.run_cycle(execute=True, approval_ids=approval_ids, config=SchedulerConfig(concurrency=2, max_tasks=2, max_seconds=10, max_attempts=1))
        self.assertEqual(result["completed"], 2)
        self.assertEqual(self.service.approval_show(approval_ids[0])["status"], "consumed")
        self.assertEqual(self.service.approval_show(approval_ids[1])["status"], "consumed")

    def test_retry_failed_is_opt_in_and_requeues_below_attempt_limit(self):
        attempts = {"count": 0}

        def fail_once(task, approval_ids):
            attempts["count"] += 1
            return {"status": "failed", "error": "temporary failure"}

        self.scheduler.dispatcher = fail_once
        self.add_task("retry-task")
        result = self.scheduler.run_cycle(execute=True, config=SchedulerConfig(concurrency=1, max_tasks=1, max_seconds=10, max_attempts=2, retry_failed=True))
        self.assertEqual(result["retry_queued"], 1)
        self.assertEqual(self.service.tasks.get("retry-task").status, "queued")

    def test_unhealthy_resources_are_not_dispatched(self):
        self.service.resources.update("local", {"availability": "unavailable"})
        self.service.resources.expire("space-bunny")
        self.add_task("health-task")
        self.scheduler.dispatcher = self.dispatcher
        result = self.scheduler.run_cycle(execute=True)
        self.assertEqual(result["selected"], 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
