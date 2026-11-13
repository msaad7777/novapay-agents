"""Optional AWS Emulator mode for the mock NovaPay estate.

The JSON files in data/novapay stay the default source: fast, offline, no Docker.
This module puts the same estate into a local AWS Emulator
(github.com/aws/aws-emulator) through the real AWS APIs, and reads it back in the
same shapes as the JSON files, so agent tools can run against either source.

    python -m novapay_agents.aws_env seed    # build the estate in the emulator
    python -m novapay_agents.aws_env check   # read it back and compare with the JSON

What goes where:
- EC2 instances, EBS volumes, a VPC, security groups and a NAT gateway (EC2 API)
- RDS instances (RDS API)
- S3 buckets with tags, encryption, public access block and bucket policy (S3 API)
- hourly metrics in the custom namespace NovaPay/Mock (CloudWatch)
- application logs in the log group /novapay/app, one stream per source (CloudWatch Logs)
- an SNS topic for on-call and one CloudWatch alarm on checkout p95 latency
- the time shift used for metrics and logs (SSM Parameter Store)

Costs, budgets, commitments and Terraform state stay in the JSON files: billing
data can't be emulated.

Safety: every call goes to a local endpoint. `require_local_endpoint` refuses to
run unless AWS_ENDPOINT_URL points at this machine, so nothing reaches real AWS.

Limits worth knowing (see the README):
- The emulator doesn't evaluate IAM. Nothing here proves least privilege.
- CloudWatch, like AWS, only accepts data points from the last two weeks, so the
  seed shifts every metric and log timestamp by a whole number of hours and stores
  the shift in SSM. `read_back` shifts them back, so readers see the original times.
- An EBS volume can't be attached to an RDS instance. vol-0c3 (the mock's database
  volume) is created unattached with the tag novapay:attached-to=db-01, and
  `read_back` reports it as in use.
- The emulator keeps everything in memory. Restart it before seeding again.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "novapay"
REGION = "ca-central-1"
NAMESPACE = "NovaPay/Mock"
LOG_GROUP = "/novapay/app"
SHIFT_PARAM = "/novapay/seed/time-shift-hours"
ATTACHED_TAG = "novapay:attached-to"
SIZE_TAG = "novapay:size-gb"
TOPIC = "novapay-oncall"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def require_local_endpoint() -> str:
    """Return AWS_ENDPOINT_URL, or stop if it isn't on this machine."""
    url = os.environ.get("AWS_ENDPOINT_URL", "")
    if urlparse(url).hostname not in LOCAL_HOSTS:
        raise SystemExit(
            f"AWS_ENDPOINT_URL={url!r} is not a local endpoint. This mode only talks to a local "
            "AWS Emulator; source scripts/emulator-env.sh first."
        )
    return url


def _clients(region: str = REGION) -> dict:
    require_local_endpoint()
    import boto3  # optional dependency: uv sync --extra aws

    names = ("ec2", "rds", "s3", "cloudwatch", "logs", "sns", "ssm")
    return {n: boto3.client(n, region_name=region) for n in names}


def _load(data_dir: Path) -> dict:
    names = ("inventory", "security", "metrics", "logs")
    return {n: json.loads((data_dir / f"{n}.json").read_text()) for n in names}


def _tags(d: dict) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in d.items()]


def _untag(tags: list[dict] | None) -> dict:
    return {t["Key"]: t["Value"] for t in tags or []}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


# ---------------------------------------------------------------- seeding


def seed(data_dir: Path = DATA_DIR, region: str = REGION, now: datetime | None = None) -> dict:
    """Create the mock estate in the emulator. Returns counts of what was created."""
    c = _clients(region)
    d = _load(data_dir)
    inv, sec = d["inventory"], d["security"]
    counts: dict[str, int] = {}

    # Network: one VPC and subnet for everything, as the mock has a single NAT gateway.
    vpc = c["ec2"].create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = c["ec2"].create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24",
                                    AvailabilityZone=f"{region}a")["Subnet"]["SubnetId"]

    sg_ids: dict[str, str] = {}
    sg_of: dict[str, list[str]] = {}
    for sg in sec["security_groups"]:
        # AWS group names can't start with "sg-", so the mock id goes in the Name tag.
        gid = c["ec2"].create_security_group(
            GroupName=f"novapay-{sg['id'].removeprefix('sg-')}", Description=f"NovaPay {sg['id']}", VpcId=vpc,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": _tags({"Name": sg["id"]})}])["GroupId"]
        c["ec2"].authorize_security_group_ingress(GroupId=gid, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": r["port"], "ToPort": r["port"],
             "IpRanges": [{"CidrIp": r["cidr"]}]} for r in sg["ingress"]])
        sg_ids[sg["id"]] = gid
        for rid in sg["attached"]:
            sg_of.setdefault(rid, []).append(gid)
    counts["security_groups"] = len(sg_ids)

    # The mock lists IMDS settings for some instances; the rest get IMDSv2 required, so the
    # emulator doesn't add IMDSv1 findings the answer key doesn't have.
    tokens = {m["id"]: m["http_tokens"] for m in sec["instances_metadata"]}
    ami = c["ec2"].describe_images(Owners=["amazon"])["Images"][0]["ImageId"]

    for r in inv:
        name_tags = _tags({"Name": r["id"], **r["tags"]})
        if r["kind"] == "ec2":
            kw = {"SecurityGroupIds": sg_of[r["id"]]} if r["id"] in sg_of else {}
            c["ec2"].run_instances(
                ImageId=ami, InstanceType=r["size"], MinCount=1, MaxCount=1, SubnetId=subnet,
                MetadataOptions={"HttpTokens": tokens.get(r["id"], "required"), "HttpEndpoint": "enabled"},
                TagSpecifications=[{"ResourceType": "instance", "Tags": name_tags}], **kw)
        elif r["kind"] == "rds":
            c["rds"].create_db_instance(
                DBInstanceIdentifier=r["id"], DBInstanceClass=r["size"], Engine="postgres",
                AllocatedStorage=100, MasterUsername="novapay", ManageMasterUserPassword=True,
                VpcSecurityGroupIds=sg_of.get(r["id"], []), Tags=_tags(r["tags"]))
        elif r["kind"] == "ebs":
            gb = int(r["size"].rsplit("-", 1)[-1])  # ebs-gp3-500
            extra = {ATTACHED_TAG: r["attached_to"]} if r["attached_to"] else {}
            c["ec2"].create_volume(AvailabilityZone=f"{region}a", Size=gb, VolumeType="gp3",
                                   TagSpecifications=[{"ResourceType": "volume",
                                                       "Tags": name_tags + _tags(extra)}])
        elif r["kind"] == "nat":
            eip = c["ec2"].allocate_address(Domain="vpc")["AllocationId"]
            c["ec2"].create_nat_gateway(SubnetId=subnet, AllocationId=eip, TagSpecifications=[
                {"ResourceType": "natgateway", "Tags": name_tags}])
        elif r["kind"] == "s3":
            _seed_bucket(c["s3"], r, next(b for b in sec["s3_buckets"] if b["name"] == r["id"]), region)
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1

    shift = _time_shift(d["metrics"], now)
    c["ssm"].put_parameter(Name=SHIFT_PARAM, Value=str(shift), Type="String", Overwrite=True)
    counts["metric_points"] = _seed_metrics(c["cloudwatch"], d["metrics"], shift)
    counts["log_events"] = _seed_logs(c["logs"], d["logs"], shift)

    topic = c["sns"].create_topic(Name=TOPIC)["TopicArn"]
    c["cloudwatch"].put_metric_alarm(
        AlarmName="checkout-p95-latency-high", AlarmActions=[topic], Namespace=NAMESPACE,
        MetricName="p95_latency_ms", Dimensions=[{"Name": "ResourceId", "Value": "web-01"}],
        Statistic="Average", Period=3600, EvaluationPeriods=1, Threshold=1000,
        ComparisonOperator="GreaterThanThreshold",
        AlarmDescription="Checkout p95 latency over 1 s for an hour; pages on-call")
    counts["alarms"] = 1

    _wait_running(c)
    return counts


def _seed_bucket(s3, r: dict, cfg: dict, region: str) -> None:
    b = r["id"]
    s3.create_bucket(Bucket=b, CreateBucketConfiguration={"LocationConstraint": region})
    s3.put_bucket_tagging(Bucket=b, Tagging={"TagSet": _tags({**r["tags"], SIZE_TAG: str(r["extra"]["gb"])})})
    algo = {"SSE-S3": "AES256", "SSE-KMS": "aws:kms"}[cfg["encryption"]]
    s3.put_bucket_encryption(Bucket=b, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algo}}]})
    public = cfg["public_read"]
    s3.put_public_access_block(Bucket=b, PublicAccessBlockConfiguration={
        k: not public for k in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")})
    if public:
        s3.put_bucket_policy(Bucket=b, Policy=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject", "Resource": f"arn:aws:s3:::{b}/*"}]}))


def _time_shift(metrics: dict, now: datetime | None) -> int:
    """Whole hours that move the last metric hour to the next full hour from now.

    CloudWatch takes points from the last two weeks up to two hours ahead. The 14-day
    window then starts 335 hours before that, which is inside the two weeks.
    """
    now = now or datetime.now(timezone.utc)
    last = max(_ts(p["ts"]) for rid, s in metrics.items() if rid != "_meta" for p in s)
    target = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return int((target - last).total_seconds() // 3600)


def _seed_metrics(cw, metrics: dict, shift: int) -> int:
    data = []
    for rid, series in metrics.items():
        if rid == "_meta":
            continue
        for p in series:
            at = _ts(p["ts"]) + timedelta(hours=shift)
            for name, value in p.items():
                if name != "ts":
                    data.append({"MetricName": name, "Dimensions": [{"Name": "ResourceId", "Value": rid}],
                                 "Timestamp": at, "Value": float(value)})
    for i in range(0, len(data), 1000):
        cw.put_metric_data(Namespace=NAMESPACE, MetricData=data[i:i + 1000])
    return len(data)


def _seed_logs(logs, events: list[dict], shift: int) -> int:
    logs.create_log_group(logGroupName=LOG_GROUP)
    by_source: dict[str, list[dict]] = {}
    for e in events:
        at = _ts(e["ts"]) + timedelta(hours=shift)
        by_source.setdefault(e["source"], []).append(
            {"timestamp": int(at.timestamp() * 1000), "message": f"{e['level']} {e['msg']}"})
    for source, evs in by_source.items():
        logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=source)
        evs.sort(key=lambda x: x["timestamp"])
        # One PutLogEvents batch can't span more than 24 hours.
        batch: list[dict] = []
        for e in evs:
            if batch and e["timestamp"] - batch[0]["timestamp"] >= 24 * 3600 * 1000:
                logs.put_log_events(logGroupName=LOG_GROUP, logStreamName=source, logEvents=batch)
                batch = []
            batch.append(e)
        logs.put_log_events(logGroupName=LOG_GROUP, logStreamName=source, logEvents=batch)
    return len(events)


def _wait_running(c, timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        states = [i["State"]["Name"] for r in c["ec2"].describe_instances()["Reservations"] for i in r["Instances"]]
        dbs = [x["DBInstanceStatus"] for x in c["rds"].describe_db_instances()["DBInstances"]]
        if all(s == "running" for s in states) and all(s == "available" for s in dbs):
            return
        time.sleep(1)
    raise TimeoutError("instances or databases did not become ready")


# ---------------------------------------------------------------- reading


def read_back(region: str = REGION) -> dict:
    """Read the estate from the emulator in the same shapes as the JSON files."""
    c = _clients(region)
    shift = int(c["ssm"].get_parameter(Name=SHIFT_PARAM)["Parameter"]["Value"])
    inv: list[dict] = []
    imds: list[dict] = []
    groups = [g for g in c["ec2"].describe_security_groups()["SecurityGroups"] if g["GroupName"] != "default"]
    sg_names = {g["GroupId"]: _untag(g.get("Tags")).get("Name", g["GroupName"]) for g in groups}
    attached: dict[str, list[str]] = {}

    def add(rid, kind, size, tags, state, attached_to=None, extra=None):
        inv.append({"id": rid, "kind": kind, "size": size, "env": tags.get("env"), "tags": tags,
                     "region": region, "attached_to": attached_to, "state": state, "extra": extra or {}})

    for res in c["ec2"].describe_instances()["Reservations"]:
        for i in res["Instances"]:
            tags = _untag(i.get("Tags"))
            rid = tags.pop("Name")
            add(rid, "ec2", i["InstanceType"], tags, i["State"]["Name"])
            imds.append({"id": rid, "http_tokens": i["MetadataOptions"]["HttpTokens"]})
            for g in i.get("SecurityGroups", []):
                if g["GroupId"] in sg_names:  # skips the VPC's default group
                    attached.setdefault(sg_names[g["GroupId"]], []).append(rid)
    for db in c["rds"].describe_db_instances()["DBInstances"]:
        state = {"available": "running"}.get(db["DBInstanceStatus"], db["DBInstanceStatus"])
        add(db["DBInstanceIdentifier"], "rds", db["DBInstanceClass"], _untag(db.get("TagList")), state)
        for g in db.get("VpcSecurityGroups", []):
            if g["VpcSecurityGroupId"] in sg_names:  # skips a default group
                attached.setdefault(sg_names[g["VpcSecurityGroupId"]], []).append(db["DBInstanceIdentifier"])
    for v in c["ec2"].describe_volumes()["Volumes"]:
        tags = _untag(v.get("Tags"))
        if "Name" not in tags:  # instance root volumes; the mock doesn't list them
            continue
        rid, on = tags.pop("Name"), tags.pop(ATTACHED_TAG, None)
        state = "in-use" if on or v.get("Attachments") else v["State"]
        add(rid, "ebs", f"ebs-{v['VolumeType']}-{v['Size']}", tags, state, attached_to=on)
    for n in c["ec2"].describe_nat_gateways()["NatGateways"]:
        if n["State"] in ("deleted", "deleting"):
            continue
        tags = _untag(n.get("Tags"))
        add(tags.pop("Name"), "nat", "nat-gateway", tags, {"available": "running"}.get(n["State"], n["State"]))

    buckets = []
    for b in c["s3"].list_buckets()["Buckets"]:
        name = b["Name"]
        tags = _untag(c["s3"].get_bucket_tagging(Bucket=name)["TagSet"])
        gb = int(tags.pop(SIZE_TAG))
        add(name, "s3", "s3-standard", tags, "running", extra={"gb": gb})
        algo = c["s3"].get_bucket_encryption(Bucket=name)["ServerSideEncryptionConfiguration"]["Rules"][0][
            "ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        try:
            public = c["s3"].get_bucket_policy_status(Bucket=name)["PolicyStatus"]["IsPublic"]
        except c["s3"].exceptions.ClientError:  # no bucket policy
            public = False
        buckets.append({"name": name, "public_read": public,
                        "encryption": {"AES256": "SSE-S3", "aws:kms": "SSE-KMS"}[algo]})

    sgs = []
    for g in groups:
        sid = sg_names[g["GroupId"]]
        ingress = [{"port": p["FromPort"], "cidr": r["CidrIp"]} for p in g["IpPermissions"] for r in p["IpRanges"]]
        sgs.append({"id": sid, "attached": sorted(attached.get(sid, [])), "ingress": ingress})

    return {
        "inventory": inv,
        "security": {"security_groups": sgs, "s3_buckets": buckets, "instances_metadata": imds},
        "metrics": _read_metrics(c["cloudwatch"], shift),
        "logs": _read_logs(c["logs"], shift),
        "alarms": [{"name": a["AlarmName"], "metric": a["MetricName"], "threshold": a["Threshold"],
                    "state": a["StateValue"], "actions": a["AlarmActions"]}
                   for a in c["cloudwatch"].describe_alarms()["MetricAlarms"]],
    }


def _read_metrics(cw, shift: int) -> dict:
    series: dict[str, dict[str, dict[str, float]]] = {}
    metrics = [m for page in cw.get_paginator("list_metrics").paginate(Namespace=NAMESPACE) for m in page["Metrics"]]
    # Hourly buckets start at StartTime, so start on a whole hour to get the points back as written.
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for i in range(0, len(metrics), 500):
        batch = metrics[i:i + 500]
        queries = [{"Id": f"m{j}", "MetricStat": {"Metric": m, "Period": 3600, "Stat": "Average"}}
                   for j, m in enumerate(batch)]
        token = None
        while True:
            kw = {"NextToken": token} if token else {}
            out = cw.get_metric_data(MetricDataQueries=queries, StartTime=now - timedelta(days=15),
                                     EndTime=now + timedelta(hours=3), **kw)
            for r in out["MetricDataResults"]:
                m = batch[int(r["Id"][1:])]
                rid = m["Dimensions"][0]["Value"]
                for t, v in zip(r["Timestamps"], r["Values"]):
                    ts = (t.astimezone(timezone.utc) - timedelta(hours=shift)).isoformat()
                    series.setdefault(rid, {}).setdefault(ts, {})[m["MetricName"]] = v
            token = out.get("NextToken")
            if not token:
                break
    return {rid: [{"ts": ts, **vals} for ts, vals in sorted(points.items())] for rid, points in series.items()}


def _read_logs(logs, shift: int) -> list[dict]:
    out = []
    for page in logs.get_paginator("filter_log_events").paginate(logGroupName=LOG_GROUP):
        for e in page["events"]:
            level, msg = e["message"].split(" ", 1)
            ts = datetime.fromtimestamp(e["timestamp"] / 1000, timezone.utc) - timedelta(hours=shift)
            out.append({"ts": ts.isoformat(), "source": e["logStreamName"], "level": level, "msg": msg})
    return sorted(out, key=lambda x: (x["ts"], x["source"], x["msg"]))


# ---------------------------------------------------------------- comparing


def compare(mock: dict, live: dict) -> dict[str, tuple[int, int]]:
    """Count how many mock items came back unchanged: {check: (matched, total)}."""
    def norm(x):
        return json.dumps(x, sort_keys=True)

    def metric_points(m):
        return {(rid, p["ts"], k, round(float(v), 6)) for rid, s in m.items() if rid != "_meta"
                for p in s for k, v in p.items() if k != "ts"}

    live_inv = {r["id"]: norm(r) for r in live["inventory"]}
    sec_m, sec_l = mock["security"], live["security"]
    live_imds = {x["id"]: x["http_tokens"] for x in sec_l["instances_metadata"]}
    live_logs = {norm(x) for x in live["logs"]}
    mock_pts, live_pts = metric_points(mock["metrics"]), metric_points(live["metrics"])
    return {
        "inventory": (sum(live_inv.get(r["id"]) == norm(r) for r in mock["inventory"]), len(mock["inventory"])),
        "security_groups": (sum(norm(g) in {norm(x) for x in sec_l["security_groups"]} for g in sec_m["security_groups"]),
                            len(sec_m["security_groups"])),
        "s3_buckets": (sum(norm(b) in {norm(x) for x in sec_l["s3_buckets"]} for b in sec_m["s3_buckets"]),
                       len(sec_m["s3_buckets"])),
        "instances_metadata": (sum(live_imds.get(x["id"]) == x["http_tokens"] for x in sec_m["instances_metadata"]),
                               len(sec_m["instances_metadata"])),
        "metric_points": (len(mock_pts & live_pts), len(mock_pts)),
        "logs": (sum(norm(e) in live_logs for e in mock["logs"]), len(mock["logs"])),
    }


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "seed":
        for k, v in seed().items():
            print(f"{k:16} {v}")
        return 0
    if cmd == "check":
        mock = _load(DATA_DIR)
        # The JSON stores timestamps as +00:00 ISO strings; normalise the mock the same way.
        for e in mock["logs"]:
            e["ts"] = _ts(e["ts"]).isoformat()
        result = compare(mock, read_back())
        for k, (ok, total) in result.items():
            print(f"{k:20} {ok}/{total}")
        return 0 if all(ok == total for ok, total in result.values()) else 1
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
