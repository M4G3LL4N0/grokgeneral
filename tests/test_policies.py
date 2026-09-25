import tempfile
import unittest

from grokgeneral.models import Project, Resource, Task
from grokgeneral.policies import PolicyEngine
from grokgeneral.storage import StateStore


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.engine = PolicyEngine(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_default_policy_is_external_and_contains_conservation_rule(self):
        policy = self.engine.load()
        self.assertTrue(self.state.policy_path.exists())
        self.assertIn("grokbot", policy["routing"]["cost_order"])
        self.assertTrue(any(rule.get("requires_approval") for rule in policy["safety"].values()))

    def test_capability_and_cost_evaluation_is_explainable(self):
        task = Task(id="t", goal="fix", required_capabilities=["coding"], metadata={"max_cost": 0})
        resource = Resource(id="free", name="free", provider="local", cost_class="free", capabilities=["coding"])
        allowed = self.engine.evaluate(task, None, resource, set())
        denied = self.engine.evaluate(task, None, Resource(id="paid", name="paid", cost_class="premium", capabilities=["coding"]), set())
        self.assertTrue(allowed.allowed)
        self.assertIn("capability.required", allowed.matches)
        self.assertFalse(denied.allowed)
        self.assertIn("cost.ceiling", denied.matches)

    def test_sensitive_actions_require_approval(self):
        decision = self.engine.authorize("network", set(), self.engine.load())
        approved = self.engine.authorize("network", {"network"}, self.engine.load())
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.requires_approval)
        self.assertTrue(approved.allowed)

    def test_resource_restrictions_and_grokbot_conservation_are_enforced(self):
        task = Task(id="t", goal="network task", required_capabilities=["research"], metadata={"network_required": True})
        restricted = Resource(id="restricted", name="restricted", provider="test", cost_class="free", capabilities=["research"], restrictions={"network": False})
        denied = self.engine.evaluate(task, None, restricted, {"network"})
        self.assertFalse(denied.allowed)
        grok = Resource(id="grok", name="grok", provider="grokbot", cost_class="premium", capabilities=["research"])
        decision = self.engine.evaluate(Task(id="t2", goal="research"), None, grok, set())
        self.assertIn("grokbot.conserve", decision.matches)

    def test_malformed_policy_is_rejected(self):
        self.state.policy_path.write_text("{bad", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.engine.load()


if __name__ == "__main__":
    unittest.main()
