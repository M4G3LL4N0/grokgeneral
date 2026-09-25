import tempfile
import unittest
from datetime import timedelta

from grokgeneral.events import EventBus
from grokgeneral.resources import ResourceRegistry
from grokgeneral.storage import StateStore
from grokgeneral.timeutil import isoformat, parse_time, utc_now


class ResourceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = StateStore(self.temp.name)
        self.state.initialize()
        self.registry = ResourceRegistry(self.state)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_add_update_and_expire(self):
        self.registry.add({"id": "local", "name": "local", "provider": "local", "cost_class": "free", "capabilities": ["testing"]})
        self.registry.update("local", {"availability": "limited", "remaining_capacity": 2})
        self.assertEqual(self.registry.get("local").remaining_capacity, 2)
        self.registry.expire("local")
        self.assertTrue(self.registry.effective(self.registry.get("local"))["expired"])

    def test_space_bunny_seed_is_free_and_idempotent(self):
        first = self.registry.seed_defaults()
        second = self.registry.seed_defaults()
        self.assertEqual(first.id, "space-bunny")
        self.assertEqual(first.cost_class, "free")
        self.assertEqual(second.expires_at, first.expires_at)
        self.assertEqual(set(first.capabilities), {"coding", "repo-analysis", "refactoring", "testing"})
        self.assertLessEqual((parse_time(first.expires_at) - utc_now()).days, 7)
        self.assertIsNotNone(self.registry.get("local"))

    def test_exhausted_resource_is_not_available(self):
        resource = self.registry.add({"id": "credit", "name": "credit", "remaining_capacity": 0, "availability": "limited"})
        effective = self.registry.effective(resource)
        self.assertTrue(effective["exhausted"])
        self.assertFalse(effective["available"])

    def test_expiration_event_is_emitted_once_when_refreshed(self):
        events = EventBus(self.state)
        registry = ResourceRegistry(self.state, events)
        registry.add({"id": "temporary", "name": "temporary", "availability": "available", "expires_at": isoformat(utc_now() - timedelta(seconds=1))})
        registry.refresh_expirations()
        registry.refresh_expirations()
        matching = [event for event in events.list() if event["event_type"] == "RESOURCE_EXPIRING" and event["payload"].get("resource") == "temporary"]
        self.assertEqual(len(matching), 1)

    def test_malformed_resource_is_rejected(self):
        with self.assertRaises(ValueError):
            self.registry.add({})


if __name__ == "__main__":
    unittest.main()
