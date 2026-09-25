import tempfile
import unittest

from grokgeneral.events import EventBus
from grokgeneral.policies import PolicyEngine
from grokgeneral.resources import ResourceRegistry
from grokgeneral.router import Router
from grokgeneral.scheduler import Scheduler
from grokgeneral.storage import StateStore
from grokgeneral.tasks import TaskRegistry


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.events = EventBus(self.state)
        self.policies = PolicyEngine(self.state)
        self.resources = ResourceRegistry(self.state, self.events)
        self.router = Router(self.state, self.policies, self.resources)
        self.tasks = TaskRegistry(self.state, self.events, self.policies, self.resources, self.router)
        self.scheduler = Scheduler(self.state, self.tasks, self.router)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_plan_is_deterministic_and_does_not_execute(self):
        self.tasks.add({"goal": "fix code", "required_capabilities": ["coding"]})
        plan = self.scheduler.plan()
        self.assertEqual(plan[0]["goal"], "fix code")
        self.assertFalse(plan[0]["execute_requested"])
        self.assertEqual(self.tasks.list()[0].status, "proposed")

    def test_tick_only_executes_when_explicit(self):
        self.tasks.add({"goal": "fix code", "metadata": {"command": ["printf", "ok"]}, "required_capabilities": ["coding"]})
        result = self.scheduler.tick(execute=True)
        self.assertEqual(result[0]["status"], "completed")
        self.assertEqual(self.tasks.list()[0].status, "completed")


if __name__ == "__main__":
    unittest.main()
