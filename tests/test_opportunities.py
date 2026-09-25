import tempfile
import unittest
from datetime import timedelta

from grokgeneral.events import EventBus
from grokgeneral.models import Project
from grokgeneral.opportunities import OpportunityEngine
from grokgeneral.policies import PolicyEngine
from grokgeneral.projects import ProjectRegistry
from grokgeneral.resources import ResourceRegistry
from grokgeneral.router import Router
from grokgeneral.storage import StateStore
from grokgeneral.tasks import TaskRegistry
from grokgeneral.timeutil import isoformat, utc_now


class OpportunityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.events = EventBus(self.state)
        self.policies = PolicyEngine(self.state)
        self.projects = ProjectRegistry(self.state, self.events)
        self.resources = ResourceRegistry(self.state, self.events)
        self.router = Router(self.state, self.policies, self.resources)
        self.tasks = TaskRegistry(self.state, self.events, self.policies, self.resources, self.router)
        self.project = self.projects.add({"id": "alpha", "name": "Alpha", "path": str(self.temp.name), "priority": 90, "status": "active"})
        self.resource = self.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "cost_class": "free", "availability": "unlimited", "capabilities": ["coding", "testing"], "expires_at": isoformat(utc_now() + timedelta(days=2))})
        self.engine = OpportunityEngine(self.state, self.projects, self.resources, self.tasks, self.router)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_free_expiring_resource_gets_suitable_backlog_work(self):
        values = self.engine.list()
        self.assertTrue(values)
        self.assertEqual(values[0]["resource"], "space")
        self.assertGreater(values[0]["score"], 0)
        self.assertIn("expiration_urgency", values[0]["components"])
        self.assertTrue(values[0]["proposed_work"])

    def test_resource_filter_and_optimize_plan_are_non_spending_by_default(self):
        plan = self.engine.optimize(max_tasks=3)
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["items"][0]["resource"], "space")
        self.assertEqual(self.tasks.list(), [])
        filtered = self.engine.list(resource="space")
        self.assertEqual(len(filtered), 1)

    def test_explicit_execute_only_queues_proposals(self):
        plan = self.engine.optimize(max_tasks=1, execute=True)
        self.assertTrue(plan["executed"])
        self.assertEqual(len(self.tasks.list()), 1)
        self.assertEqual(self.tasks.list()[0].status, "queued")


if __name__ == "__main__":
    unittest.main()
