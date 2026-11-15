"""Guardrails for NovaPay agents: an action policy, a human approval queue and an audit log.

Agents never change anything directly. They *propose* an action. Code, not a
model, decides what happens to the proposal:

- ``denied``: on the deny list, not on the allow list, or unsafe as asked
  (for example deleting a volume with no approved snapshot).
- ``invalid``: unknown resource, wrong kind of resource, or missing parameters.
- ``needs_approval``: a person must approve it. Prod changes are marked high risk.
- ``rate_limited``: the agent already has too many proposals waiting.

Only a person can approve, through the command line (``python -m
novapay_agents.guardrails``). The MCP server has no approve tool. Approving
records the decision; nothing is executed in this slice.

Every tool call and decision goes into an append-only audit log. Each line
carries the hash of the line before it, so an edited or deleted line is
detected by ``AuditLog.verify``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import tools

DEFAULT_STATE = tools.REPO / "state"
# One full governance sweep of the mock estate proposes 16 fixes; 20 fits it and still caps a runaway loop.
MAX_PENDING_PER_AGENT = 20


def max_pending() -> int:
    return int(os.environ.get("NOVAPAY_MAX_PENDING", MAX_PENDING_PER_AGENT))

# Never allowed, whoever asks. Reason shown to the agent.
DENY = {
    "terminate_instance": "Terminating instances is never done by an agent.",
    "delete_db": "Deleting databases is never done by an agent.",
    "delete_bucket": "Deleting buckets is never done by an agent.",
    "delete_snapshot": "Snapshots are the recovery path; agents never delete them.",
    "delete_logs": "Logs are evidence; agents never delete them.",
    "disable_encryption": "Encryption is never turned off by an agent.",
    "make_bucket_public": "Making a bucket public is never done by an agent.",
    "purchase_savings_plan": "Purchases are never made by an agent. Draft the model for a person instead.",
    "purchase_reserved_instance": "Purchases are never made by an agent. Draft the model for a person instead.",
    "modify_iam": "IAM changes are out of scope for agents.",
    "disable_alarm": "Alarms are never silenced by an agent.",
    "run_shell": "Free-form commands are not allowed; use a named action.",
}

# Allowed to propose. kinds: resource kinds it applies to (None = any). params: required parameters.
ALLOW = {
    "stop_instance": {"kinds": {"ec2"}, "params": []},
    "start_instance": {"kinds": {"ec2"}, "params": []},
    "restart_service": {"kinds": {"ec2"}, "params": ["service"]},
    "resize_instance": {"kinds": {"ec2"}, "params": ["size"]},
    "resize_db": {"kinds": {"rds"}, "params": ["size"]},
    "set_db_pool_size": {"kinds": {"ec2"}, "params": ["pool_size"]},
    "rollback_deploy": {"kinds": {"ec2"}, "params": ["version"]},
    "snapshot_volume": {"kinds": {"ebs"}, "params": []},
    "delete_volume": {"kinds": {"ebs"}, "params": []},
    "add_tag": {"kinds": None, "params": ["key", "value"]},
    "block_public_access": {"kinds": {"s3"}, "params": []},
    "remove_sg_rule": {"kinds": {"security_group"}, "params": ["port", "cidr"]},
    "add_sg_rule": {"kinds": {"security_group"}, "params": ["port", "cidr"]},
    "require_imdsv2": {"kinds": {"ec2"}, "params": []},
    "add_vpc_endpoint": {"kinds": {"nat"}, "params": ["service"]},
    "cleanup_disk": {"kinds": {"ec2"}, "params": ["path"]},
    "import_to_terraform": {"kinds": None, "params": []},
}


def state_dir() -> Path:
    path = Path(os.environ.get("NOVAPAY_STATE", DEFAULT_STATE))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- audit log -------------------------------------------------------------------

class AuditLog:
    """Append-only JSON-lines log where each line holds the previous line's hash."""

    GENESIS = "0" * 64

    def __init__(self, path: Path | None = None):
        self.path = path or state_dir() / "audit.jsonl"

    def _last(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, self.GENESIS
        last = None
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, self.GENESIS
        rec = json.loads(last)
        return rec["seq"], rec["hash"]

    @staticmethod
    def _digest(rec: dict) -> str:
        body = {k: v for k, v in rec.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    def append(self, actor: str, event: str, **details) -> dict:
        seq, prev = self._last()
        rec = {"seq": seq + 1, "ts": _now(), "actor": actor, "event": event, "details": details, "prev": prev}
        rec["hash"] = self._digest(rec)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    def verify(self) -> dict:
        """Check the chain. Returns ok and, if broken, the first bad line number."""
        if not self.path.exists():
            return {"ok": True, "lines": 0}
        prev, n = self.GENESIS, 0
        with open(self.path, encoding="utf-8") as f:
            for n, line in enumerate((l for l in f if l.strip()), start=1):
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    return {"ok": False, "lines": n, "first_bad_line": n, "why": "not JSON"}
                if rec.get("seq") != n:
                    return {"ok": False, "lines": n, "first_bad_line": n, "why": "sequence gap"}
                if rec.get("prev") != prev:
                    return {"ok": False, "lines": n, "first_bad_line": n, "why": "previous hash mismatch"}
                if rec.get("hash") != self._digest(rec):
                    return {"ok": False, "lines": n, "first_bad_line": n, "why": "line was changed"}
                prev = rec["hash"]
        return {"ok": True, "lines": n}


# --- policy -------------------------------------------------------------------------

@dataclass
class Decision:
    decision: str  # denied | invalid | needs_approval
    reason: str
    risk: str | None = None


def _approved_snapshot_exists(queue: "ApprovalQueue", volume: str) -> bool:
    return any(r["action"] == "snapshot_volume" and r["resource"] == volume and r["status"] == "approved"
               for r in queue.all())


def evaluate(action: str, resource: str, params: dict | None, reason: str,
             queue: "ApprovalQueue | None" = None) -> Decision:
    """Decide what happens to a proposed action. Pure policy: no side effects."""
    params = params or {}
    if action in DENY:
        return Decision("denied", DENY[action])
    if action not in ALLOW:
        return Decision("denied", f"{action!r} is not on the allow list. Allowed: {sorted(ALLOW)}")
    if not reason or not reason.strip():
        return Decision("invalid", "Give a reason: the evidence that this action fixes a real problem.")
    res = tools.get_resource(resource)
    if res is None:
        return Decision("invalid", f"Unknown resource {resource!r}.")
    spec = ALLOW[action]
    if spec["kinds"] is not None and res["kind"] not in spec["kinds"]:
        return Decision("invalid", f"{action} applies to {sorted(spec['kinds'])}, not {res['kind']}.")
    missing = [p for p in spec["params"] if p not in params or params[p] in (None, "")]
    if missing:
        return Decision("invalid", f"Missing parameters: {missing}")

    if action == "delete_volume":
        if res.get("state") == "in-use" or res.get("attached_to"):
            return Decision("denied", f"{resource} is attached ({res.get('attached_to')}); never delete an attached volume.")
        if queue is None or not _approved_snapshot_exists(queue, resource):
            return Decision("denied", f"Snapshot {resource} first: propose snapshot_volume and wait for approval.")
    if action == "add_sg_rule" and str(params["cidr"]) in ("0.0.0.0/0", "::/0") and int(params["port"]) != 443:
        return Decision("denied", "Only port 443 may be opened to the internet.")
    if action == "set_db_pool_size":
        try:
            size = int(params["pool_size"])
        except (TypeError, ValueError):
            return Decision("invalid", "pool_size must be a whole number.")
        if not 1 <= size <= 100:
            return Decision("invalid", "pool_size must be between 1 and 100.")
    if action == "cleanup_disk" and not str(params["path"]).startswith(("/var/tmp/", "/tmp/")):
        return Decision("denied", "Cleanup is limited to /var/tmp/ and /tmp/.")

    risk = "high" if res.get("env") == "prod" else "low"
    return Decision("needs_approval", "A person must approve this change.", risk)


# --- approval queue -----------------------------------------------------------------

class ApprovalQueue:
    """Proposals waiting for a person, stored as JSON in the state folder."""

    def __init__(self, path: Path | None = None, audit: AuditLog | None = None):
        self.path = path or state_dir() / "approvals.json"
        self.audit = audit or AuditLog(self.path.parent / "audit.jsonl")

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, rows: list[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, request_id: str) -> dict | None:
        return next((r for r in self.all() if r["id"] == request_id), None)

    def propose(self, agent: str, action: str, resource: str, params: dict | None, reason: str) -> dict:
        params = params or {}
        d = evaluate(action, resource, params, reason, self)
        result = {"decision": d.decision, "reason": d.reason}
        if d.decision == "needs_approval":
            rows = self.all()
            same = next((r for r in rows if r["status"] == "pending" and r["action"] == action
                         and r["resource"] == resource and r["params"] == params), None)
            if same:
                result.update(decision="needs_approval", request_id=same["id"], status="pending",
                              reason="Already waiting for approval.", risk=same["risk"])
            elif sum(1 for r in rows if r["status"] == "pending" and r["agent"] == agent) >= max_pending():
                result = {"decision": "rate_limited",
                          "reason": f"{agent} already has {max_pending()} proposals waiting."}
            else:
                rid = f"REQ-{len(rows) + 1:04d}"
                rows.append({"id": rid, "agent": agent, "action": action, "resource": resource, "params": params,
                             "reason": reason, "risk": d.risk, "status": "pending", "created": _now(),
                             "decided_by": None, "decided_at": None, "note": None})
                self._save(rows)
                result.update(request_id=rid, status="pending", risk=d.risk)
        self.audit.append(agent, "propose_action", action=action, resource=resource, params=params,
                          reason=reason, result=result)
        return result

    def decide(self, request_id: str, approve: bool, by: str, note: str = "") -> dict:
        """Called by a person from the command line, never through MCP."""
        if not by or by.strip().lower() in {"agent", "governance-agent", "oncall-agent"} or by.endswith("-agent"):
            raise PermissionError("Approvals must name a person.")
        rows = self.all()
        req = next((r for r in rows if r["id"] == request_id), None)
        if req is None:
            raise KeyError(request_id)
        if req["status"] != "pending":
            raise ValueError(f"{request_id} is already {req['status']}.")
        req.update(status="approved" if approve else "denied", decided_by=by, decided_at=_now(), note=note or None)
        self._save(rows)
        self.audit.append(f"human:{by}", "approve" if approve else "deny", request_id=request_id, note=note)
        return req


# --- command line, for people ---------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m novapay_agents.guardrails",
                                 description="Review NovaPay agent proposals. For people, not agents.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Show proposals waiting for approval")
    for name in ("approve", "deny"):
        p = sub.add_parser(name)
        p.add_argument("request_id")
        p.add_argument("--by", required=True, help="Your name")
        p.add_argument("--note", default="")
    sub.add_parser("verify", help="Check the audit log's hash chain")
    args = ap.parse_args(argv)

    q = ApprovalQueue()
    if args.cmd == "list":
        pending = [r for r in q.all() if r["status"] == "pending"]
        for r in pending:
            print(f"{r['id']}  [{r['risk']}]  {r['agent']}: {r['action']} {r['resource']} {r['params'] or ''}")
            print(f"        reason: {r['reason']}")
        print(f"{len(pending)} waiting")
        return 0
    if args.cmd == "verify":
        res = AuditLog().verify()
        print(json.dumps(res))
        return 0 if res["ok"] else 1
    try:
        r = q.decide(args.request_id, args.cmd == "approve", args.by, args.note)
    except (KeyError, ValueError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{r['id']} {r['status']} by {r['decided_by']}. Nothing is executed in this version.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
