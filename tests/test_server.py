import asyncio
import unittest

from mcp import Client

from novapay_agents.guardrails import ApprovalQueue
from novapay_agents.server import build_server

from _fixture import Estate


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.estate = Estate().__enter__()

    def tearDown(self):
        self.estate.__exit__()

    def run_client(self, fn):
        async def go():
            async with Client(build_server()) as c:
                return await fn(c)
        return asyncio.run(go())

    def test_no_tool_can_approve_execute_or_delete(self):
        async def names(c):
            return [t.name for t in (await c.list_tools()).tools]
        tools = self.run_client(names)
        self.assertIn("propose_action", tools)
        for word in ("approve", "execute", "apply", "delete", "run"):
            self.assertFalse([t for t in tools if word in t], word)

    def test_read_tools_are_marked_read_only(self):
        async def ann(c):
            return {t.name: t.annotations.read_only_hint for t in (await c.list_tools()).tools}
        marks = self.run_client(ann)
        self.assertFalse(marks.pop("propose_action"))
        self.assertTrue(all(marks.values()))

    def test_propose_and_status_round_trip(self):
        async def flow(c):
            r = (await c.call_tool("propose_action", {"action": "stop_instance", "resource": "gpu-train-01",
                                                     "reason": "0% GPU"})).structured_content
            ApprovalQueue().decide(r["request_id"], True, by="saad")
            return (await c.call_tool("get_request_status", {"request_id": r["request_id"]})).structured_content
        s = self.run_client(flow)
        self.assertEqual((s["status"], s["decided_by"]), ("approved", "saad"))

    def test_bad_input_returns_an_error_not_a_crash(self):
        async def bad(c):
            return (await c.call_tool("get_metrics", {"resource": "web-01", "metric": "gpu"})).structured_content
        self.assertIn("error", self.run_client(bad))

    def test_every_call_is_audited(self):
        async def calls(c):
            await c.call_tool("budget_status", {})
            await c.call_tool("get_security", {})
            await c.call_tool("propose_action", {"action": "delete_db", "resource": "db-01", "reason": "x"})
        self.run_client(calls)
        from novapay_agents.guardrails import AuditLog
        log = AuditLog()
        self.assertEqual(log.verify(), {"ok": True, "lines": 3})


if __name__ == "__main__":
    unittest.main()
