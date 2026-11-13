"""Tests for the optional AWS Emulator mode.

The unit tests need no emulator and no boto3. The round-trip test runs only when
NOVAPAY_AWS_EMULATOR=1 and an emulator is running with a fresh seed:

    source scripts/emulator-env.sh
    uv run python -m novapay_agents.aws_env seed
    NOVAPAY_AWS_EMULATOR=1 uv run python -m pytest tests/test_aws_env.py
"""

import copy
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from novapay_agents import aws_env


def test_refuses_a_non_local_endpoint(monkeypatch):
    for url in ("", "https://ec2.ca-central-1.amazonaws.com", "https://127.0.0.1.example.com:9297"):
        monkeypatch.setenv("AWS_ENDPOINT_URL", url)
        with pytest.raises(SystemExit):
            aws_env.require_local_endpoint()


def test_accepts_a_local_endpoint(monkeypatch):
    for url in ("https://127.0.0.1:9297", "https://localhost:9297", "http://[::1]:5000"):
        monkeypatch.setenv("AWS_ENDPOINT_URL", url)
        assert aws_env.require_local_endpoint() == url


def test_time_shift_keeps_the_window_inside_two_weeks():
    metrics = aws_env._load(aws_env.DATA_DIR)["metrics"]
    points = [datetime.fromisoformat(p["ts"]) for k, s in metrics.items() if k != "_meta" for p in s]
    for minute in (0, 1, 30, 59):
        now = datetime(2026, 11, 13, 20, minute, 30, tzinfo=timezone.utc)
        shift = timedelta(hours=aws_env._time_shift(metrics, now))
        assert min(points) + shift > now - timedelta(days=14)
        assert max(points) + shift <= now + timedelta(hours=2)


def test_compare_counts_changes():
    mock = aws_env._load(aws_env.DATA_DIR)
    live = copy.deepcopy(mock)
    assert all(ok == total for ok, total in aws_env.compare(mock, live).values())
    live["inventory"][0]["tags"]["team"] = "someone-else"
    live["security"]["s3_buckets"][0]["public_read"] = False
    live["metrics"]["web-01"][0]["cpu_pct"] += 1
    live["logs"].pop()
    result = aws_env.compare(mock, live)
    assert result["inventory"] == (14, 15)
    assert result["s3_buckets"] == (1, 2)
    assert result["metric_points"][0] == result["metric_points"][1] - 1
    assert result["logs"] == (30, 31)


@pytest.mark.skipif(os.environ.get("NOVAPAY_AWS_EMULATOR") != "1", reason="needs a running, freshly seeded AWS Emulator")
def test_round_trip_through_the_emulator():
    mock = aws_env._load(aws_env.DATA_DIR)
    live = aws_env.read_back()
    result = aws_env.compare(mock, live)
    assert all(ok == total for ok, total in result.values()), json.dumps(result)
    # Instances the mock doesn't list get IMDSv2 required, so they add no IMDS findings.
    listed = {x["id"] for x in mock["security"]["instances_metadata"]}
    assert all(x["http_tokens"] == "required" for x in live["security"]["instances_metadata"] if x["id"] not in listed)
