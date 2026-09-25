import tempfile
import unittest
from datetime import timedelta

from grokgeneral.approvals import ApprovalRegistry
from grokgeneral.errors import ApprovalError
from grokgeneral.events import EventBus
from grokgeneral.models import Task
from grokgeneral.storage import StateStore
from grokgeneral.timeutil import isoformat, utc_now


class ApprovalRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.events = EventBus(self.state)
        self.approvals = ApprovalRegistry(self.state, self.events)
        self.task = Task(id="task-1", goal="modify fixture", project="project-1", attempts=0)
        self.other_task = Task(id="task-2", goal="other", project="project-1", attempts=0)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_approval_is_scoped_to_task_attempt_actions_and_payload(self):
        approval = self.approvals.request(self.task, 1, {"modify", "validate"}, "hash-a")
        self.approvals.approve(approval["id"])
        with self.assertRaises(ApprovalError):
            self.approvals.consume([approval["id"]], self.other_task, 1, {"modify", "validate"}, "hash-a")
        with self.assertRaises(ApprovalError):
            self.approvals.consume([approval["id"]], self.task, 2, {"modify", "validate"}, "hash-a")
        with self.assertRaises(ApprovalError):
            self.approvals.consume([approval["id"]], self.task, 1, {"push"}, "hash-a")

    def test_approved_request_is_single_use(self):
        approval = self.approvals.request(self.task, 1, {"validate"}, "hash-a")
        self.approvals.approve(approval["id"])
        self.assertEqual(self.approvals.consume([approval["id"]], self.task, 1, {"validate"}, "hash-a"), {"validate"})
        with self.assertRaises(ApprovalError):
            self.approvals.consume([approval["id"]], self.task, 1, {"validate"}, "hash-a")

    def test_rejected_and_expired_requests_are_not_usable(self):
        rejected = self.approvals.request(self.task, 1, {"push"})
        self.approvals.reject(rejected["id"], "not authorized")
        self.assertEqual(self.approvals.show(rejected["id"])["status"], "rejected")
        expired = self.approvals.request(self.task, 1, {"push"}, expires_at=isoformat(utc_now() - timedelta(seconds=1)))
        self.approvals.expire()
        self.assertEqual(self.approvals.show(expired["id"])["status"], "expired")

    def test_consume_requires_complete_action_coverage(self):
        modify = self.approvals.request(self.task, 1, {"modify"}, "hash-a")
        validate = self.approvals.request(self.task, 1, {"validate"}, "hash-a")
        self.approvals.approve(modify["id"])
        self.approvals.approve(validate["id"])
        self.assertEqual(self.approvals.consume([modify["id"], validate["id"]], self.task, 1, {"modify", "validate"}, "hash-a"), {"modify", "validate"})

    def test_list_filters_by_status_and_task(self):
        self.approvals.request(self.task, 1, {"modify"})
        self.approvals.request(self.other_task, 1, {"push"})
        self.assertEqual(len(self.approvals.list()), 2)
        self.assertEqual(len(self.approvals.list(task_id="task-1")), 1)
        self.assertEqual(self.approvals.list(status="pending")[0]["task_id"], "task-1")


if __name__ == "__main__":
    unittest.main()
