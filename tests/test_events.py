import tempfile
import unittest

from grokgeneral.events import EventBus, PROJECT_DISCOVERED, TASK_CREATED, RESOURCE_RESET, MODEL_CHANGED, PROJECT_BLOCKED, PROJECT_COMPLETED, DEPENDENCY_CHANGED, SECURITY_ALERT
from grokgeneral.storage import StateStore


class EventBusTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = StateStore(self.directory.name)
        self.state.initialize()
        self.bus = EventBus(self.state)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_emit_persists_redacted_payload_and_order(self):
        first = self.bus.emit(PROJECT_DISCOVERED, {"project": "one", "api_key": "hidden"})
        second = self.bus.emit(PROJECT_DISCOVERED, {"project": "two"})

        events = self.bus.list()
        self.assertEqual([event["id"] for event in events], [first["id"], second["id"]])
        self.assertEqual(first["payload"]["api_key"], "[REDACTED]")
        self.assertNotIn("hidden", str(events))

        reopened = EventBus(StateStore(self.directory.name))
        self.assertEqual([event["id"] for event in reopened.list()], [first["id"], second["id"]])

    def test_local_subscription_delivers_once_per_event(self):
        received = []
        subscription = self.bus.subscribe(TASK_CREATED, "collector", received.append)
        event = self.bus.emit(TASK_CREATED, {"task_id": "task-1"})

        self.bus.deliver(event["id"])
        self.bus.deliver(event["id"])

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["id"], event["id"])
        self.assertEqual(subscription["event_type"], TASK_CREATED)
        persisted = self.state.list_records("subscriptions")
        self.assertEqual(len(persisted), 1)
        self.assertIsNotNone(self.state.get_record("events", event["id"])["delivered_at"])

    def test_nonmatching_subscription_is_not_delivered(self):
        received = []
        self.bus.subscribe(TASK_CREATED, "collector", received.append)
        event = self.bus.emit(PROJECT_DISCOVERED, {"project": "one"})

        self.bus.deliver(event["id"])

        self.assertEqual(received, [])

    def test_global_event_types_are_available(self):
        for event_type in (RESOURCE_RESET, MODEL_CHANGED, PROJECT_BLOCKED, PROJECT_COMPLETED, DEPENDENCY_CHANGED, SECURITY_ALERT):
            self.assertIn(event_type, __import__("grokgeneral.events", fromlist=["EVENT_TYPES"]).EVENT_TYPES)

    def test_list_limit_returns_the_most_recent_events_in_order(self):
        events = [self.bus.emit(PROJECT_DISCOVERED, {"index": index}) for index in range(3)]

        listed = self.bus.list(limit=2)

        self.assertEqual([event["id"] for event in listed], [events[1]["id"], events[2]["id"]])


if __name__ == "__main__":
    unittest.main()
