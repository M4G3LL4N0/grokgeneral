import tempfile
import unittest
from datetime import timedelta

from grokgeneral.cache import Cache
from grokgeneral.events import EventBus
from grokgeneral.policies import PolicyEngine
from grokgeneral.resources import ResourceRegistry
from grokgeneral.router import Router
from grokgeneral.storage import StateStore
from grokgeneral.tasks import TaskRegistry
from grokgeneral.timeutil import isoformat, utc_now


class ExpiryRerouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.events = EventBus(self.state)
        self.cache = Cache(self.state)
        self.policies = PolicyEngine(self.state)
        self.resources = ResourceRegistry(self.state, self.events, self.cache)
        self.resources.add({"id": "space", "name": "space", "provider": "opencode", "executor": "Space Bunny", "model": "opencode/space-bunny-free", "cost_class": "free", "capabilities": ["coding"], "availability": "unlimited", "expires_at": isoformat(utc_now() + timedelta(days=2))})
        self.resources.add({"id": "local", "name": "local", "provider": "local", "executor": "local", "cost_class": "free", "capabilities": ["coding"], "availability": "available"})
        self.router = Router(self.state, self.policies, self.resources, self.cache)
        self.tasks = TaskRegistry(self.state, self.events, self.policies, self.resources, self.router)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_expiration_invalidates_relevant_caches_once(self):
        task = self.tasks.add({"goal": "coding", "required_capabilities": ["coding"]})
        self.router.route(task)
        self.assertTrue(self.cache.inspect())
        self.resources.update("space", {"expires_at": isoformat(utc_now() - timedelta(seconds=1)), "availability": "unavailable"})
        changed = self.resources.refresh_expirations()
        self.assertEqual(changed[0]["resource"], "space")
        self.assertFalse([item for item in self.cache.inspect() if item["kind"] in {"route", "opportunity"}])
        self.assertEqual(len([event for event in self.events.list() if event["event_type"] == "RESOURCE_EXPIRING" and event["payload"].get("resource") == "space"]), 1)

    def test_queued_task_reroutes_to_capable_fallback(self):
        task = self.tasks.add({"goal": "coding", "required_capabilities": ["coding"]})
        self.tasks.route(task.id)
        self.resources.update("space", {"expires_at": isoformat(utc_now() - timedelta(seconds=1)), "availability": "unavailable"})
        self.resources.refresh_expirations()
        rerouted = self.tasks.reroute_queued()
        self.assertEqual(len(rerouted), 1)
        self.assertEqual(rerouted[0].assigned_executor, "local")
        self.assertEqual(rerouted[0].status, "queued")

    def test_expired_and_expiring_resource_filters(self):
        self.resources.add({"id": "later", "name": "later", "availability": "available", "expires_at": isoformat(utc_now() + timedelta(days=10))})
        self.assertEqual([item.id for item in self.resources.list(expiring=True)], ["space"])
        self.assertEqual([item.id for item in self.resources.list(expired=True)], [])


if __name__ == "__main__":
    unittest.main()
