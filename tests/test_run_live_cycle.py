from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger
import run_live_cycle as cycle


class LiveCycleLeaseTests(unittest.TestCase):
    def test_cycle_lease_is_single_owner_and_releasable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            first = cycle.acquire_cycle_lease(connection)
            self.assertIsNotNone(first)
            self.assertIsNone(cycle.acquire_cycle_lease(connection))
            cycle.release_cycle_lease(connection, first)
            second = cycle.acquire_cycle_lease(connection)
            self.assertIsNotNone(second)
            cycle.release_cycle_lease(connection, second)
            connection.close()


if __name__ == "__main__":
    unittest.main()
