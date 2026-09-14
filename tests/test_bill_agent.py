# tests/test_bill_agent.py
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from billguard.agents.bills import BILL_AGENT_SPEC, build_bill_registry
from billguard.bills import BillService


class RegistryTests(unittest.TestCase):
    def test_five_read_tools_registered_with_schemas(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = build_bill_registry(BillService(Path(temp)))
            self.assertEqual(
                ("bill_overview", "bill_compare", "bill_anomalies", "bill_search", "bill_samples"),
                tuple(registry.names()))
            tool = registry.get("bill_anomalies")
            self.assertIn("dimension", tool.parameters["properties"])
            self.assertEqual(["spike", "duplicate", "price_hike", "outlier"],
                             tool.parameters["properties"]["dimension"]["enum"])

    def test_spec_forbids_freeform_execution(self):
        self.assertIn("不执行任意 SQL", BILL_AGENT_SPEC.instructions)



if __name__ == "__main__":
    unittest.main()
