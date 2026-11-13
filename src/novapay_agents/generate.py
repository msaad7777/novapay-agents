"""Seeded mock NovaPay environment.

Builds a fictional AWS estate for the fintech "NovaPay": inventory, 30 days of
daily cost rows (FOCUS-style columns), 14 days of hourly metrics, logs,
commitments, Terraform state and a security snapshot. Problems are planted on
purpose and listed in an answer key, so agents can be scored for recall and
precision. Decoys look suspicious but are fine, to measure false findings.

Every price here is a mock rate chosen for the exercise, not an AWS list price.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SEED = 20261112
START = date(2026, 10, 1)
COST_DAYS = 30
METRIC_DAYS = 14
ACCOUNT_ID = "000000000000"  # fictional

# Mock daily rates in USD (not AWS prices).
DAILY_RATE = {
    "t3.medium": 1.00,
    "m6i.large": 2.30,
    "m6i.xlarge": 4.60,
    "c6i.2xlarge": 8.20,
    "r6g.2xlarge": 9.70,
    "db.r6g.large": 6.20,
    "db.r6g.2xlarge": 24.80,
    "g5.xlarge": 24.10,
    "ebs-gp3-500": 1.33,
    "nat-gateway": 1.08,
    "s3-standard": 0.0,  # billed by usage below
}
NAT_PER_GB = 0.045
S3_PER_GB_MONTH = 0.023


@dataclass
class Resource:
    id: str
    kind: str
    size: str
    env: str
    tags: dict[str, str]
    region: str = "ca-central-1"
    attached_to: str | None = None
    state: str = "running"
    extra: dict = field(default_factory=dict)


def _resources() -> list[Resource]:
    t = lambda team, env, svc: {"team": team, "env": env, "service": svc}  # noqa: E731
    return [
        Resource("web-01", "ec2", "m6i.xlarge", "prod", t("checkout", "prod", "checkout-web")),
        Resource("web-02", "ec2", "m6i.xlarge", "prod", t("checkout", "prod", "checkout-web")),
        Resource("batch-01", "ec2", "c6i.2xlarge", "prod", t("payouts", "prod", "settlement-batch")),
        Resource("payments-worker-01", "ec2", "m6i.large", "prod", t("payments", "prod", "payments-worker")),
        Resource("db-01", "rds", "db.r6g.large", "prod", t("payments", "prod", "core-db")),
        Resource("reporting-db", "rds", "db.r6g.2xlarge", "prod", t("finance", "prod", "reporting")),
        Resource("dev-sandbox-01", "ec2", "m6i.xlarge", "dev", t("platform", "dev", "sandbox")),
        Resource("gpu-train-01", "ec2", "g5.xlarge", "dev", t("risk-ml", "dev", "fraud-model")),
        Resource("analytics-01", "ec2", "r6g.2xlarge", "prod", {"env": "prod"}),  # no team tag
        Resource("vol-0a1", "ebs", "ebs-gp3-500", "dev", t("platform", "dev", "sandbox"), state="available"),
        Resource("vol-0b2", "ebs", "ebs-gp3-500", "dev", {"env": "dev"}, state="available"),
        Resource("vol-0c3", "ebs", "ebs-gp3-500", "prod", t("payments", "prod", "core-db"), attached_to="db-01", state="in-use"),
        Resource("nat-01", "nat", "nat-gateway", "prod", t("platform", "prod", "network")),
        Resource("novapay-exports", "s3", "s3-standard", "prod", t("finance", "prod", "exports"), extra={"gb": 800}),
        Resource("novapay-logs", "s3", "s3-standard", "prod", t("platform", "prod", "logs"), extra={"gb": 4200}),
    ]


BUDGETS = {"checkout": 400, "payouts": 350, "payments": 600, "finance": 900,
           "platform": 400, "risk-ml": 800}


def _costs(res: list[Resource], rng: random.Random) -> list[dict]:
    rows = []
    for d in range(COST_DAYS):
        day = START + timedelta(days=d)
        for r in res:
            if r.kind == "s3":
                cost = r.extra["gb"] * S3_PER_GB_MONTH / 30
                usage = "storage"
            elif r.kind == "nat":
                # Planted C3: batch-01 pulls settlement files from S3 through the
                # NAT gateway (no S3 gateway endpoint) from day 10 onwards.
                gb = rng.uniform(40, 60) + (rng.uniform(1900, 2100) if d >= 10 else 0)
                cost = DAILY_RATE["nat-gateway"] + gb * NAT_PER_GB
                usage = f"{gb:.0f} GB processed"
            else:
                cost = DAILY_RATE[r.size] * rng.uniform(0.98, 1.02)
                usage = "24 h"
            rows.append({
                "ChargePeriodStart": day.isoformat(),
                "BillingAccountId": ACCOUNT_ID,
                "ServiceName": {"ec2": "Amazon EC2", "rds": "Amazon RDS", "ebs": "Amazon EBS",
                                "nat": "Amazon VPC", "s3": "Amazon S3"}[r.kind],
                "ResourceId": r.id,
                "SkuDescription": r.size,
                "UsageDescription": usage,
                "BilledCost": round(cost, 2),
                "EffectiveCost": round(cost, 2),
                "ChargeCategory": "Usage",
                "PricingCategory": "On-Demand",
                "Tags": dict(r.tags),
            })
    return rows


def _metrics(rng: random.Random) -> dict[str, list[dict]]:
    """Hourly metrics for the last METRIC_DAYS days of the cost window."""
    start = datetime.combine(START + timedelta(days=COST_DAYS - METRIC_DAYS), datetime.min.time(), timezone.utc)
    hours = METRIC_DAYS * 24
    incident_start = 10 * 24 + 14  # day 10, 14:00 UTC, of the metric window
    out: dict[str, list[dict]] = {}

    def series(rid: str, fn) -> None:
        out[rid] = [{"ts": (start + timedelta(hours=h)).isoformat(), **fn(h)} for h in range(hours)]

    def web(h: int) -> dict:
        busy = 9 <= (h % 24) <= 21
        slow = incident_start <= h < incident_start + 5
        return {"cpu_pct": round(rng.uniform(35, 55) if busy else rng.uniform(10, 20), 1),
                "mem_pct": round(rng.uniform(50, 60), 1),
                "disk_pct": 41.0,
                "p95_latency_ms": round(rng.uniform(2400, 3100) if slow else rng.uniform(180, 260)),
                "error_rate_pct": round(rng.uniform(4, 7) if slow else rng.uniform(0.1, 0.3), 2)}

    series("web-01", web)
    series("web-02", web)

    def db(h: int) -> dict:
        nightly = 1 <= (h % 24) <= 3  # decoy D1: nightly settlement batch, expected
        slow = incident_start <= h < incident_start + 5
        return {"cpu_pct": round(rng.uniform(80, 90) if nightly else rng.uniform(25, 40), 1),
                "connections": 100 if slow else rng.randint(30, 55),
                "max_connections": 100,
                "free_storage_gb": 210}

    series("db-01", db)

    def batch(h: int) -> dict:
        # Planted I2: disk fills from day 12 and hits 100% on day 13 at 03:00.
        fill_start = 12 * 24
        disk = 62.0 if h < fill_start else min(100.0, 62.0 + (h - fill_start) * 1.4)
        return {"cpu_pct": round(rng.uniform(70, 90) if 1 <= (h % 24) <= 4 else rng.uniform(3, 8), 1),
                "mem_pct": round(rng.uniform(30, 40), 1), "disk_pct": round(disk, 1)}

    series("batch-01", batch)

    def worker(h: int) -> dict:
        # Planted I3: memory leak, climbs ~0.6%/h from 40% and restarts at 97%.
        mem = 40 + (h % 95) * 0.6
        return {"cpu_pct": round(rng.uniform(20, 30), 1), "mem_pct": round(mem, 1), "disk_pct": 33.0}

    series("payments-worker-01", worker)
    series("dev-sandbox-01", lambda h: {"cpu_pct": round(rng.uniform(0.3, 1.5), 1), "mem_pct": 8.0, "disk_pct": 12.0})
    series("gpu-train-01", lambda h: {"cpu_pct": round(rng.uniform(0.5, 2), 1), "gpu_util_pct": 0.0, "mem_pct": 6.0})
    series("reporting-db", lambda h: {"cpu_pct": round(rng.uniform(3, 8), 1), "connections": rng.randint(1, 4), "max_connections": 400})
    series("analytics-01", lambda h: {"cpu_pct": round(rng.uniform(45, 70), 1), "mem_pct": round(rng.uniform(60, 75), 1)})
    out["_meta"] = [{"window_start": start.isoformat(), "incident_checkout_start_hour": incident_start}]
    return out


def _logs(metrics: dict) -> list[dict]:
    start = datetime.fromisoformat(metrics["_meta"][0]["window_start"])
    at = lambda h, m=0: (start + timedelta(hours=h, minutes=m)).isoformat()  # noqa: E731
    inc = metrics["_meta"][0]["incident_checkout_start_hour"]
    logs = []
    for i in range(12):
        logs.append({"ts": at(inc, i * 20), "source": "web-01", "level": "ERROR",
                     "msg": "checkout: timeout acquiring DB connection from pool (waited 3000 ms)"})
        logs.append({"ts": at(inc, i * 20 + 3), "source": "db-01", "level": "ERROR",
                     "msg": 'FATAL: remaining connection slots are reserved; too many connections for role "checkout_app"'})
    logs.append({"ts": at(inc - 1, 50), "source": "deploy", "level": "INFO",
                 "msg": "checkout-web v2.14.0 deployed: DB pool size per instance raised 20 -> 60"})
    logs.append({"ts": at(13 * 24 + 3), "source": "batch-01", "level": "ERROR",
                 "msg": "settlement-batch: write failed: [Errno 28] No space left on device: '/var/tmp/settlement'"})
    logs.append({"ts": at(13 * 24 + 3, 1), "source": "batch-01", "level": "WARN",
                 "msg": "/var/tmp/settlement holds 9 days of uncompressed export files (no cleanup job)"})
    for k in range(3):
        logs.append({"ts": at(95 * (k + 1)), "source": "payments-worker-01", "level": "ERROR",
                     "msg": "payments-worker killed by OOM killer (rss 7.6 GiB); systemd restarting"})
    logs.append({"ts": at(26), "source": "db-01", "level": "INFO",  # decoy D1 context
                 "msg": "nightly settlement batch started (expected high CPU 01:00-03:00 UTC)"})
    return sorted(logs, key=lambda x: x["ts"])


def _security(res: list[Resource]) -> dict:
    return {
        "security_groups": [
            {"id": "sg-web", "attached": ["web-01", "web-02"], "ingress": [{"port": 443, "cidr": "0.0.0.0/0"}]},  # decoy D2: public HTTPS is intended
            {"id": "sg-batch", "attached": ["batch-01"], "ingress": [{"port": 22, "cidr": "0.0.0.0/0"}]},  # planted S1
            {"id": "sg-db", "attached": ["db-01"], "ingress": [{"port": 5432, "cidr": "10.0.0.0/16"}]},
        ],
        "s3_buckets": [
            {"name": "novapay-exports", "public_read": True, "encryption": "SSE-S3"},  # planted S2
            {"name": "novapay-logs", "public_read": False, "encryption": "SSE-KMS"},
        ],
        "instances_metadata": [
            {"id": "web-01", "http_tokens": "optional"},  # planted S3: IMDSv1 allowed
            {"id": "web-02", "http_tokens": "required"},
            {"id": "batch-01", "http_tokens": "required"},
            {"id": "payments-worker-01", "http_tokens": "required"},
        ],
    }


def _commitments() -> dict:
    return {"savings_plans": [], "reserved_instances": [],
            "note": "No commitments. Steady 24/7 prod compute is all on-demand."}


def _terraform_state(res: list[Resource]) -> dict:
    # Planted C9 / drift: analytics-01 and nat-01 are not in Terraform (made in the console).
    in_code = [r.id for r in res if r.id not in {"analytics-01", "nat-01"} and r.kind != "ebs"]
    return {"version": 4, "resources": [{"address": f"aws_resource.{rid.replace('-', '_')}", "id": rid} for rid in in_code]}


ANSWER_KEY = {
    "governance": [
        {"id": "C1", "category": "cost", "resource": "dev-sandbox-01", "finding": "Idle dev instance: CPU under 2% for 14 days", "fix": "Stop it, or schedule it for office hours"},
        {"id": "C2", "category": "cost", "resource": "reporting-db", "finding": "Oversized RDS: db.r6g.2xlarge at 3-8% CPU and 1-4 connections", "fix": "Downsize to db.r6g.large after a test"},
        {"id": "C3", "category": "cost", "resource": "nat-01", "finding": "NAT data processing jumped about 2 TB/day from day 10: batch-01 reads S3 through the NAT gateway", "fix": "Add an S3 gateway VPC endpoint"},
        {"id": "C4", "category": "cost", "resource": "gpu-train-01", "finding": "GPU instance left running with 0% GPU use", "fix": "Stop it; run training as a scheduled job"},
        {"id": "C5", "category": "cost", "resource": "analytics-01,vol-0b2", "finding": "Spend with no team tag (unowned)", "fix": "Find the owner and tag it; enforce the tag policy"},
        {"id": "C6", "category": "cost", "resource": "vol-0a1,vol-0b2", "finding": "Unattached EBS volumes", "fix": "Snapshot, then delete after owner sign-off"},
        {"id": "C7", "category": "cost", "resource": "team:platform", "finding": "Team platform over its monthly budget (driven by NAT)", "fix": "Fix C3; review the budget"},
        {"id": "C8", "category": "cost", "resource": "prod compute", "finding": "Steady 24/7 prod compute has no Savings Plan coverage", "fix": "Model a 1-year Compute Savings Plan for the steady baseline (draft only, never buy)"},
        {"id": "C9", "category": "governance", "resource": "analytics-01,nat-01", "finding": "Resources not in Terraform (console-made)", "fix": "Import into code"},
        {"id": "S1", "category": "security", "resource": "sg-batch", "finding": "SSH (22) open to 0.0.0.0/0 on batch-01", "fix": "Remove the rule; use SSM Session Manager"},
        {"id": "S2", "category": "security", "resource": "novapay-exports", "finding": "S3 bucket allows public read", "fix": "Block public access"},
        {"id": "S3", "category": "security", "resource": "web-01", "finding": "IMDSv1 allowed (http_tokens optional)", "fix": "Require IMDSv2"},
    ],
    "incidents": [
        {"id": "I1", "resource": "web-01,db-01", "symptom": "Checkout p95 latency over 2.4 s and errors 4-7% for 5 hours", "root_cause": "checkout-web v2.14.0 raised the DB pool from 20 to 60 per instance; two instances exhaust db-01 max_connections (100)", "fix": "Roll back the pool size or add a connection pooler (RDS Proxy)"},
        {"id": "I2", "resource": "batch-01", "symptom": "Settlement batch failed: no space left on device", "root_cause": "9 days of uncompressed exports in /var/tmp/settlement with no cleanup job", "fix": "Archive old files to S3 and add a cleanup job"},
        {"id": "I3", "resource": "payments-worker-01", "symptom": "Worker killed by OOM roughly every 95 hours", "root_cause": "Memory leak: memory climbs steadily until the OOM killer restarts it", "fix": "Restart is a stopgap; file a bug with the heap profile"},
    ],
    "decoys": [
        {"id": "D1", "resource": "db-01", "looks_like": "CPU 80-90% every night", "why_fine": "Expected nightly settlement batch, 01:00-03:00 UTC"},
        {"id": "D2", "resource": "sg-web", "looks_like": "Port open to 0.0.0.0/0", "why_fine": "Public HTTPS (443) on the checkout web tier is intended"},
        {"id": "D3", "resource": "analytics-01", "looks_like": "Large instance", "why_fine": "CPU 45-70%, sized correctly; its only problem is the missing tag (C5)"},
    ],
}


def generate(out_dir: Path, seed: int = SEED) -> dict[str, Path]:
    rng = random.Random(seed)
    res = _resources()
    metrics = _metrics(rng)
    files = {
        "inventory.json": [r.__dict__ for r in res],
        "costs.json": _costs(res, rng),
        "budgets.json": BUDGETS,
        "metrics.json": metrics,
        "logs.json": _logs(metrics),
        "security.json": _security(res),
        "commitments.json": _commitments(),
        "terraform_state.json": _terraform_state(res),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, data in files.items():
        p = out_dir / name
        p.write_text(json.dumps(data, indent=1))
        written[name] = p
    # The answer key is kept outside the data directory the agents can read.
    key_dir = out_dir.parent / "answer_key"
    key_dir.mkdir(parents=True, exist_ok=True)
    (key_dir / "answer_key.json").write_text(json.dumps(ANSWER_KEY, indent=1))
    written["answer_key.json"] = key_dir / "answer_key.json"
    return written


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2] / "data"
    for name, p in generate(root / "novapay").items():
        print(f"wrote {p.relative_to(root.parent)}")
