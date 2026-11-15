import json
import os
import unittest

from novapay_agents import guardrails as g

from _fixture import Estate


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.estate = Estate().__enter__()
        self.q = g.ApprovalQueue()

    def tearDown(self):
        self.estate.__exit__()

    def test_deny_list_wins_even_with_a_reason(self):
        for action in g.DENY:
            self.assertEqual(g.evaluate(action, "web-01", {}, "good reason").decision, "denied", action)

    def test_unknown_action_is_denied(self):
        self.assertEqual(g.evaluate("scale_to_zero", "web-01", {}, "x").decision, "denied")

    def test_invalid_inputs(self):
        self.assertEqual(g.evaluate("stop_instance", "web-99", {}, "x").decision, "invalid")
        self.assertEqual(g.evaluate("resize_db", "web-01", {"size": "s"}, "x").decision, "invalid")
        self.assertEqual(g.evaluate("resize_instance", "web-01", {}, "x").decision, "invalid")
        self.assertEqual(g.evaluate("stop_instance", "web-01", {}, " ").decision, "invalid")
        self.assertEqual(g.evaluate("set_db_pool_size", "web-01", {"pool_size": 0}, "x").decision, "invalid")

    def test_prod_is_high_risk(self):
        self.assertEqual(g.evaluate("stop_instance", "web-01", {}, "x").risk, "high")
        self.assertEqual(g.evaluate("stop_instance", "dev-sandbox-01", {}, "x").risk, "low")
        self.assertEqual(g.evaluate("remove_sg_rule", "sg-batch", {"port": 22, "cidr": "0.0.0.0/0"}, "x").risk, "high")

    def test_internet_ingress_only_on_443(self):
        self.assertEqual(g.evaluate("add_sg_rule", "sg-db", {"port": 5432, "cidr": "0.0.0.0/0"}, "x").decision, "denied")
        self.assertEqual(g.evaluate("add_sg_rule", "sg-web", {"port": 443, "cidr": "0.0.0.0/0"}, "x").decision,
                         "needs_approval")

    def test_delete_volume_needs_an_approved_snapshot(self):
        self.assertEqual(self.q.propose("a", "delete_volume", "vol-0a1", {}, "x")["decision"], "denied")
        snap = self.q.propose("a", "snapshot_volume", "vol-0a1", {}, "before delete")
        self.assertEqual(self.q.propose("a", "delete_volume", "vol-0a1", {}, "x")["decision"], "denied")  # pending isn't enough
        self.q.decide(snap["request_id"], True, by="saad")
        self.assertEqual(self.q.propose("a", "delete_volume", "vol-0a1", {}, "x")["decision"], "needs_approval")
        self.assertEqual(self.q.propose("a", "delete_volume", "vol-0c3", {}, "x")["decision"], "denied")  # attached


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.estate = Estate().__enter__()
        self.q = g.ApprovalQueue()

    def tearDown(self):
        self.estate.__exit__()

    def test_duplicate_returns_the_same_request(self):
        a = self.q.propose("a", "stop_instance", "dev-sandbox-01", {}, "idle")
        b = self.q.propose("a", "stop_instance", "dev-sandbox-01", {}, "idle again")
        self.assertEqual(a["request_id"], b["request_id"])
        self.assertEqual(len(self.q.all()), 1)

    def test_rate_limit(self):
        os.environ["NOVAPAY_MAX_PENDING"] = "2"
        out = [self.q.propose("a", "add_tag", "web-01", {"key": "k", "value": str(i)}, "x")["decision"] for i in range(3)]
        self.assertEqual(out, ["needs_approval", "needs_approval", "rate_limited"])
        self.assertEqual(self.q.propose("b", "add_tag", "web-01", {"key": "k", "value": "b"}, "x")["decision"],
                         "needs_approval")  # per agent

    def test_only_people_approve_and_only_once(self):
        r = self.q.propose("a", "stop_instance", "dev-sandbox-01", {}, "idle")
        for who in ("agent", "governance-agent", "", "oncall-agent"):
            with self.assertRaises(PermissionError):
                self.q.decide(r["request_id"], True, by=who)
        self.q.decide(r["request_id"], False, by="saad", note="keep for demo")
        with self.assertRaises(ValueError):
            self.q.decide(r["request_id"], True, by="saad")
        self.assertEqual(self.q.get(r["request_id"])["status"], "denied")

    def test_cli(self):
        r = self.q.propose("a", "stop_instance", "dev-sandbox-01", {}, "idle")
        self.assertEqual(g.main(["approve", r["request_id"], "--by", "saad"]), 0)
        self.assertEqual(self.q.get(r["request_id"])["status"], "approved")
        self.assertEqual(g.main(["approve", r["request_id"], "--by", "saad"]), 2)
        self.assertEqual(g.main(["verify"]), 0)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.estate = Estate().__enter__()
        self.log = g.AuditLog()

    def tearDown(self):
        self.estate.__exit__()

    def _lines(self):
        return self.log.path.read_text().splitlines()

    def test_chain_verifies(self):
        for i in range(5):
            self.log.append("a", "tool_call", n=i)
        self.assertEqual(self.log.verify(), {"ok": True, "lines": 5})

    def test_edit_is_caught(self):
        for i in range(5):
            self.log.append("a", "tool_call", n=i)
        lines = self._lines()
        rec = json.loads(lines[2])
        rec["details"]["n"] = 99
        lines[2] = json.dumps(rec, sort_keys=True)
        self.log.path.write_text("\n".join(lines) + "\n")
        res = self.log.verify()
        self.assertFalse(res["ok"])
        self.assertEqual(res["first_bad_line"], 3)

    def test_deleted_line_is_caught(self):
        for i in range(5):
            self.log.append("a", "tool_call", n=i)
        lines = self._lines()
        self.log.path.write_text("\n".join(lines[:1] + lines[2:]) + "\n")
        self.assertFalse(self.log.verify()["ok"])


if __name__ == "__main__":
    unittest.main()
