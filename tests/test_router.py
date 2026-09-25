import tempfile
import unittest
from datetime import timedelta

from grokgeneral.cache import Cache
from grokgeneral.models import Project, Resource, Task
from grokgeneral.policies import PolicyEngine
from grokgeneral.resources import ResourceRegistry
from grokgeneral.router import Router
from grokgeneral.storage import StateStore
from grokgeneral.timeutil import isoformat, utc_now


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.policies = PolicyEngine(self.state)
        self.resources = ResourceRegistry(self.state)
        self.router = Router(self.state, self.policies, self.resources, Cache(self.state))

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def add_resource(self, identifier, **changes):
        return self.resources.add({"id": identifier, "name": identifier, "provider": "test", "executor": identifier, "capabilities": ["coding"], "cost_class": "free", "availability": "available", **changes})

    def test_free_resource_wins_and_fallbacks_are_explainable(self):
        self.add_resource("paid", cost_class="premium", marginal_cost=2)
        self.add_resource("free")
        decision = self.router.route(Task(id="t", goal="fix", required_capabilities=["coding"]))
        self.assertEqual(decision.executor, "free")
        self.assertEqual(decision.estimated_cost_class, "free")
        self.assertTrue(decision.fallbacks)
        self.assertIn("cost", decision.reason.lower())
        self.assertIn("free", [item["id"] for item in decision.candidates])

    def test_expired_and_exhausted_resources_are_filtered(self):
        self.add_resource("expired", expires_at=isoformat(utc_now() - timedelta(seconds=1)))
        self.add_resource("exhausted", remaining_capacity=0)
        self.add_resource("usable")
        decision = self.router.route(Task(id="t", goal="fix", required_capabilities=["coding"]))
        self.assertEqual(decision.executor, "usable")
        candidate_ids = [item["id"] for item in decision.candidates]
        self.assertNotIn("expired", candidate_ids)
        self.assertNotIn("exhausted", candidate_ids)

    def test_project_preference_overrides_only_when_capable(self):
        self.add_resource("free")
        self.add_resource("preferred", cost_class="cheap", marginal_cost=0.1)
        project = Project(id="p", name="Project", path="/tmp/p", preferred_executors=["preferred"])
        decision = self.router.route(Task(id="t", goal="fix", required_capabilities=["coding"]), project=project)
        self.assertEqual(decision.executor, "preferred")
        self.assertIn("preferred_executor", decision.reason.lower() + " ".join(decision.policy_matches))

    def test_context_limit_and_cache_key_are_honored(self):
        self.add_resource("small", context_limit=10)
        decision = self.router.route(Task(id="t", goal="fix", required_capabilities=["coding"], metadata={"context_tokens": 100}))
        self.assertEqual(decision.executor, "local")
        cache = self.router.cache
        key = cache.key("route", {"task": "fix", "resource": "small"})
        cache.put(key, {"answer": "cached"}, "route")
        second = self.router.route(Task(id="t", goal="fix", required_capabilities=["coding"]))
        self.assertFalse(second.cache_hit)

    def test_no_capable_resource_returns_safe_unassigned_decision(self):
        decision = self.router.route(Task(id="t", goal="fix", required_capabilities=["unavailable-capability"]))
        self.assertEqual(decision.executor, "unassigned")
        self.assertEqual(decision.provider, "none")
        self.assertIn("no capable", decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
