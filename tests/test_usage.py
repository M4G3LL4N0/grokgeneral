import tempfile
import unittest

from grokgeneral.errors import ValidationError
from grokgeneral.storage import StateStore
from grokgeneral.usage import UsageLedger


class UsageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = StateStore(self.directory.name)
        self.state.initialize()
        self.ledger = UsageLedger(self.state)

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def test_record_preserves_source_and_optional_values(self):
        record = self.ledger.record(
            source="measured",
            units=2,
            cost=1.25,
            project="project-a",
            resource="resource-a",
            task_id="task-a",
            metadata={"token": "hidden"},
        )

        self.assertEqual(record["source"], "measured")
        self.assertEqual(record["units"], 2.0)
        self.assertEqual(record["cost"], 1.25)
        self.assertEqual(record["project_id"], "project-a")
        self.assertEqual(record["metadata"]["token"], "[REDACTED]")
        self.assertEqual(self.state.get_record("usage", record["id"])["source"], "measured")

    def test_only_supported_source_labels_are_accepted(self):
        for source in ("measured", "user-entered", "estimated", "unknown"):
            self.ledger.record(source=source, units=1)
        with self.assertRaises(ValidationError):
            self.ledger.record(source="guessed", units=1)

    def test_list_filters_by_project_resource_and_task(self):
        self.ledger.record(source="measured", units=1, project="a", resource="r1", task_id="t1")
        self.ledger.record(source="estimated", units=2, project="a", resource="r2", task_id="t2")
        self.ledger.record(source="measured", units=3, project="b", resource="r1", task_id="t1")

        self.assertEqual(len(self.ledger.list(project="a")), 2)
        self.assertEqual(len(self.ledger.list(resource="r1")), 2)
        self.assertEqual(len(self.ledger.list(task_id="t1")), 2)
        self.assertEqual(len(self.ledger.list(project="a", resource="r2", task_id="t2")), 1)

    def test_summary_keeps_unknown_totals_unknown(self):
        self.ledger.record(source="measured", units=2, cost=1.0, project="a")
        self.ledger.record(source="estimated", units=3, cost=2.0, project="a")
        self.ledger.record(source="unknown", project="a")

        summary = self.ledger.summary(project="a")

        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["known_units"], 5.0)
        self.assertEqual(summary["known_cost"], 3.0)
        self.assertIsNone(summary["units"])
        self.assertIsNone(summary["cost"])
        self.assertEqual(summary["unknown_units"], 1)
        self.assertEqual(summary["unknown_cost"], 1)
        self.assertEqual(summary["by_source"]["measured"]["count"], 1)
        self.assertEqual(summary["by_source"]["unknown"]["count"], 1)

    def test_empty_summary_does_not_invent_usage(self):
        summary = self.ledger.summary()
        self.assertEqual(summary["count"], 0)
        self.assertIsNone(summary["units"])
        self.assertIsNone(summary["cost"])
        self.assertEqual(summary["known_units"], 0.0)
        self.assertEqual(summary["known_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
