import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from novapay_agents.generate import generate


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        generate(self.tmp / "novapay")
        self.d = self.tmp / "novapay"
        self.load = lambda n: json.loads((self.d / n).read_text())

    def test_same_seed_same_data(self):
        other = Path(tempfile.mkdtemp())
        generate(other / "novapay")
        for f in self.d.iterdir():
            self.assertEqual(f.read_text(), (other / "novapay" / f.name).read_text(), f.name)

    def test_answer_key_is_outside_agent_data(self):
        self.assertFalse((self.d / "answer_key.json").exists())
        self.assertTrue((self.tmp / "answer_key" / "answer_key.json").exists())

    def test_planted_cost_signals(self):
        costs = self.load("costs.json")
        nat = [r["BilledCost"] for r in costs if r["ResourceId"] == "nat-01"]
        self.assertGreater(sum(nat[10:]) / 20, 20 * sum(nat[:10]) / 10)  # C3 jump
        spend = defaultdict(float)
        for r in costs:
            spend[r["Tags"].get("team")] += r["BilledCost"]
        budgets = self.load("budgets.json")
        over = {t for t, b in budgets.items() if spend[t] > b}
        self.assertEqual(over, {"platform"})  # C7 only
        self.assertIn(None, spend)  # C5 untagged spend exists

    def test_planted_incident_signals(self):
        m = self.load("metrics.json")
        inc = m["_meta"][0]["incident_checkout_start_hour"]
        self.assertEqual(m["db-01"][inc]["connections"], m["db-01"][inc]["max_connections"])
        self.assertGreater(m["web-01"][inc]["p95_latency_ms"], 2000)
        self.assertEqual(max(x["disk_pct"] for x in m["batch-01"]), 100.0)
        logs = self.load("logs.json")
        self.assertTrue(any("No space left" in l["msg"] for l in logs))
        self.assertTrue(any("OOM" in l["msg"] for l in logs))

    def test_security_and_drift(self):
        s = self.load("security.json")
        ssh = [g for g in s["security_groups"] if any(i["port"] == 22 and i["cidr"] == "0.0.0.0/0" for i in g["ingress"])]
        self.assertEqual([g["id"] for g in ssh], ["sg-batch"])
        state_ids = {r["id"] for r in self.load("terraform_state.json")["resources"]}
        self.assertNotIn("analytics-01", state_ids)
        self.assertIn("web-01", state_ids)


if __name__ == "__main__":
    unittest.main()
