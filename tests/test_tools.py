import os
import unittest

from novapay_agents import tools

from _fixture import Estate


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.estate = Estate().__enter__()

    def tearDown(self):
        self.estate.__exit__()

    def test_inventory_filters(self):
        self.assertEqual(tools.list_inventory()["total"], 15)
        self.assertEqual({r["id"] for r in tools.list_inventory(team="untagged")["rows"]}, {"analytics-01", "vol-0b2"})
        self.assertTrue(all(r["kind"] == "rds" for r in tools.list_inventory(kind="rds")["rows"]))

    def test_budget_status_flags_only_platform(self):
        over = [r["team"] for r in tools.budget_status()["rows"] if r["status"] == "over"]
        self.assertEqual(over, ["platform"])

    def test_cost_summary_totals_match_rows(self):
        rows = tools.load("costs")
        total = round(sum(r["EffectiveCost"] for r in rows), 2)
        by_team = round(sum(r["cost"] for r in tools.cost_summary("team")["rows"]), 2)
        self.assertAlmostEqual(total, by_team, places=1)
        with self.assertRaises(tools.ToolInputError):
            tools.cost_summary("colour")

    def test_metrics_summary_and_errors(self):
        m = tools.get_metrics("dev-sandbox-01", "cpu_pct")
        self.assertEqual(m["count"], 336)
        self.assertLess(m["max"], 2)
        with self.assertRaises(tools.ToolInputError) as e:
            tools.get_metrics("dev-sandbox-01", "gpu_util_pct")
        self.assertIn("cpu_pct", str(e.exception))  # tells the agent what exists
        with self.assertRaises(tools.ToolInputError):
            tools.get_metrics("_meta", "window_start")

    def test_peaks_find_the_checkout_incident(self):
        peaks = tools.find_metric_peaks("web-01", "p95_latency_ms", 2400)
        self.assertGreaterEqual(peaks["total"], 1)
        self.assertGreaterEqual(peaks["rows"][0]["hours"], 4)

    def test_logs_are_marked_untrusted(self):
        out = tools.search_logs(contains="OOM")
        self.assertGreaterEqual(out["total"], 3)
        self.assertIn("untrusted", out["note"])

    def test_drift(self):
        d = tools.terraform_drift()
        self.assertIn("analytics-01", d["not_in_terraform"])
        self.assertIn("nat-01", d["not_in_terraform"])

    def test_output_is_capped(self):
        out = tools.get_metrics("web-01", "cpu_pct", points=True)["points"]
        self.assertEqual(len(out["rows"]), tools.MAX_ROWS)
        self.assertEqual(out["truncated"], 336 - tools.MAX_ROWS)

    def test_answer_key_is_unreachable(self):
        with self.assertRaises(tools.ToolInputError):
            tools.load("answer_key")
        os.environ["NOVAPAY_DATA"] = str(self.estate.tmp / "answer_key")
        with self.assertRaises(RuntimeError):
            tools.data_dir()


if __name__ == "__main__":
    unittest.main()
