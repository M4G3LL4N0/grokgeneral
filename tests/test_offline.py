import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grokgeneral.service import GrokGeneral


class OfflineTests(unittest.TestCase):
    def test_core_works_without_provider_keys_or_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {}, clear=True):
                service = GrokGeneral(Path(directory) / "state", offline=True)
                service.initialize()
                self.assertEqual({item.id for item in service.resources.list()}, {"local", "space-bunny"})
                decision = service.route_goal("audit a repository")
                self.assertIn(decision["executor"], {"Space Bunny", "local", "unassigned"})
                service.close()


if __name__ == "__main__":
    unittest.main()
