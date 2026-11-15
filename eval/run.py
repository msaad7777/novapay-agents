"""Measure the MCP server and guardrails over real stdio.

1. Policy: 36 hand-labelled proposals (eval/actions.jsonl) through propose_action.
2. Snapshot rule: delete_volume is denied until a person approves a snapshot.
3. Audit log: the hash chain verifies, and an edited copy is caught.
4. Evidence: each planted problem and decoy in the answer key is visible through
   the read tools. The answer key is read here, by the scorer, never by the server.

Writes eval/results/results.json. Run:  uv run python eval/run.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

from mcp import Client, StdioServerParameters

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from novapay_agents.guardrails import ApprovalQueue, AuditLog  # noqa: E402

ANSWER_KEY = REPO / "data" / "answer_key" / "answer_key.json"


def evidence_checks():
    """(answer-key id, tool, args, predicate on the result)."""
    def cpu_avg_below(x):
        return lambda r: r["avg"] < x
    return [
        ("C1", "get_metrics", {"resource": "dev-sandbox-01", "metric": "cpu_pct"}, lambda r: r["max"] < 2),
        ("C2", "get_metrics", {"resource": "reporting-db", "metric": "connections"}, lambda r: r["max"] <= 4),
        ("C3", "cost_summary", {"group_by": "service"},
         lambda r: any(x["key"] == "Amazon VPC" and x["cost"] > 1000 for x in r["rows"])),
        ("C4", "get_metrics", {"resource": "gpu-train-01", "metric": "gpu_util_pct"}, lambda r: r["max"] == 0),
        ("C5", "list_inventory", {"team": "untagged"},
         lambda r: "analytics-01" in {x["id"] for x in r["rows"]}),
        ("C6", "list_inventory", {"kind": "ebs"},
         lambda r: {x["id"] for x in r["rows"] if x["state"] == "available"} == {"vol-0a1", "vol-0b2"}),
        ("C7", "budget_status", {}, lambda r: [x["team"] for x in r["rows"] if x["status"] == "over"] == ["platform"]),
        ("C8", "get_commitments", {}, lambda r: not r["savings_plans"] and not r["reserved_instances"]),
        ("C9", "terraform_drift", {}, lambda r: {"analytics-01", "nat-01"} <= set(r["not_in_terraform"])),
        ("S1", "get_security", {}, lambda r: any(g["id"] == "sg-batch" and {"port": 22, "cidr": "0.0.0.0/0"} in g["ingress"]
                                                for g in r["security_groups"])),
        ("S2", "get_security", {}, lambda r: any(b["name"] == "novapay-exports" and b["public_read"] for b in r["s3_buckets"])),
        ("S3", "get_security", {}, lambda r: any(i["id"] == "web-01" and i["http_tokens"] == "optional"
                                                for i in r["instances_metadata"])),
        ("I1", "search_logs", {"source": "deploy", "contains": "pool"}, lambda r: r["total"] >= 1),
        ("I1", "find_metric_peaks", {"resource": "web-01", "metric": "p95_latency_ms", "threshold": 2400},
         lambda r: r["total"] >= 1),
        ("I1", "get_metrics", {"resource": "db-01", "metric": "connections"}, lambda r: r["max"] >= 100),
        ("I2", "search_logs", {"source": "batch-01", "contains": "no space"}, lambda r: r["total"] >= 1),
        ("I2", "get_metrics", {"resource": "batch-01", "metric": "disk_pct"}, lambda r: r["max"] >= 95),
        ("I3", "search_logs", {"contains": "OOM"}, lambda r: r["total"] >= 3),
        ("D1", "find_metric_peaks", {"resource": "db-01", "metric": "cpu_pct", "threshold": 80},
         lambda r: r["total"] >= 7),
        ("D1", "search_logs", {"source": "db-01", "contains": "expected"}, lambda r: r["total"] >= 1),
        ("D2", "get_security", {}, lambda r: any(g["id"] == "sg-web" and g["ingress"] == [{"port": 443, "cidr": "0.0.0.0/0"}]
                                                for g in r["security_groups"])),
        ("D3", "get_metrics", {"resource": "analytics-01", "metric": "cpu_pct"}, cpu_avg_below(100)),
    ]


async def main() -> int:
    state = Path(tempfile.mkdtemp(prefix="novapay-eval-"))
    env = dict(os.environ, NOVAPAY_STATE=str(state), NOVAPAY_AGENT="eval-agent")
    params = StdioServerParameters(command=sys.executable, args=["-m", "novapay_agents.server"], env=env,
                                   cwd=str(REPO))
    cases = [json.loads(l) for l in (REPO / "eval" / "actions.jsonl").read_text().splitlines() if l.strip()]
    latencies, policy_rows = [], []

    async with Client(params) as c:
        tool_names = sorted(t.name for t in (await c.list_tools()).tools)

        async def call(name, args):
            t0 = time.perf_counter()
            r = await c.call_tool(name, args)
            latencies.append((time.perf_counter() - t0) * 1000)
            return r.structured_content

        for case in cases:
            out = await call("propose_action", {k: case[k] for k in ("action", "resource", "params", "reason")})
            ok = out["decision"] == case["expect"] and (case["expect"] != "needs_approval" or out.get("risk") == case.get("risk"))
            policy_rows.append({"id": case["id"], "expect": case["expect"], "got": out["decision"],
                                "risk": out.get("risk"), "ok": ok, "reason": out["reason"]})

        # Snapshot rule: a person approves the snapshot of vol-0a1, then the delete may be proposed.
        before = await call("propose_action", {"action": "delete_volume", "resource": "vol-0a1", "params": {},
                                               "reason": "unattached"})
        snap = next(r for r in ApprovalQueue(state / "approvals.json").all()
                    if r["action"] == "snapshot_volume" and r["resource"] == "vol-0a1")
        ApprovalQueue(state / "approvals.json").decide(snap["id"], True, by="eval-reviewer")
        after = await call("propose_action", {"action": "delete_volume", "resource": "vol-0a1", "params": {},
                                              "reason": "unattached; snapshot approved"})
        status = await call("get_request_status", {"request_id": snap["id"]})

        evidence = []
        for item, tool, args, pred in evidence_checks():
            out = await call(tool, args)
            evidence.append({"id": item, "tool": tool, "args": args, "visible": bool(pred(out))})

    queue_rows = ApprovalQueue(state / "approvals.json").all()
    denied_ids = {r["id"] for r in policy_rows if r["expect"] in ("denied", "invalid")}
    key = lambda x: (x["action"], x["resource"], json.dumps(x["params"], sort_keys=True), x["reason"])  # noqa: E731
    denied_keys = {key(c) for c in cases if c["id"] in denied_ids}
    leaked = [r for r in queue_rows if key(r) in denied_keys]

    audit = AuditLog(state / "audit.jsonl")
    chain = audit.verify()
    tampered = state / "audit-tampered.jsonl"
    lines = (state / "audit.jsonl").read_text().splitlines()
    rec = json.loads(lines[4])
    rec["details"]["result"] = {"decision": "approved"}
    lines[4] = json.dumps(rec, sort_keys=True)
    tampered.write_text("\n".join(lines) + "\n")
    tamper = AuditLog(tampered).verify()
    dropped = state / "audit-dropped.jsonl"
    dropped.write_text("\n".join(lines[:2] + lines[3:]) + "\n")
    drop = AuditLog(dropped).verify()

    ids = {x["id"] for x in json.loads(ANSWER_KEY.read_text())["governance"]} | \
          {x["id"] for x in json.loads(ANSWER_KEY.read_text())["incidents"]} | \
          {x["id"] for x in json.loads(ANSWER_KEY.read_text())["decoys"]}
    visible = {i for i in ids if all(e["visible"] for e in evidence if e["id"] == i) and any(e["id"] == i for e in evidence)}

    lat = sorted(latencies)
    results = {
        "tools": tool_names,
        "policy": {"cases": len(policy_rows), "correct": sum(r["ok"] for r in policy_rows),
                   "by_expected": {k: sum(1 for r in policy_rows if r["expect"] == k) for k in ("needs_approval", "denied", "invalid")},
                   "denied_or_invalid_that_reached_the_queue": len(leaked),
                   "wrong": [r for r in policy_rows if not r["ok"]]},
        "snapshot_rule": {"delete_before_snapshot_approved": before["decision"],
                          "delete_after_snapshot_approved": after["decision"],
                          "snapshot_status": status["status"], "approved_by": status["decided_by"]},
        "audit": {"lines": chain["lines"], "chain_ok": chain["ok"],
                  "edited_line_caught": not tamper["ok"], "edited_line_reported": tamper.get("first_bad_line"),
                  "deleted_line_caught": not drop["ok"]},
        "evidence": {"answer_key_items": len(ids), "visible_through_tools": len(visible),
                     "missing": sorted(ids - visible), "checks": len(evidence)},
        "latency_ms_stdio": {"calls": len(lat), "p50": round(statistics.median(lat), 1),
                             "p95": round(lat[int(0.95 * (len(lat) - 1))], 1), "max": round(lat[-1], 1)},
    }
    out_dir = REPO / "eval" / "results"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=1))
    (out_dir / "policy_cases.json").write_text(json.dumps(policy_rows, indent=1))
    # Rate limit on its own: with a limit of 3, the 4th distinct proposal waits.
    limit_state = Path(tempfile.mkdtemp(prefix="novapay-limit-"))
    os.environ["NOVAPAY_MAX_PENDING"] = "3"
    q = ApprovalQueue(limit_state / "approvals.json")
    burst = [q.propose("loop-agent", "add_tag", "web-01", {"key": "k", "value": str(i)}, "test")["decision"]
             for i in range(5)]
    del os.environ["NOVAPAY_MAX_PENDING"]
    results["rate_limit"] = {"limit": 3, "decisions": burst}
    (out_dir / "results.json").write_text(json.dumps(results, indent=1))
    shutil.rmtree(limit_state)
    shutil.rmtree(state)
    print(json.dumps({k: v for k, v in results.items() if k != "tools"}, indent=1))
    return 0 if results["policy"]["correct"] == len(policy_rows) and not leaked else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
