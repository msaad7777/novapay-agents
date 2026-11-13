# NovaPay agents

A governance agent (FinOps lead) and an on-call agent for a fictional fintech, NovaPay, built for the AI Agents
course final projects. Everything runs on a seeded mock environment with planted problems and an answer key, so
results can be measured. Mock prices are not AWS list prices.

**Work in progress.** The mock environment and its optional AWS Emulator mode are done. The MCP server and the
agents are next, so there are no agent results yet.

![architecture](docs/architecture.png)

## The mock estate

`python -m novapay_agents.generate` writes the estate with a fixed seed:

- **Inventory:** 15 resources (7 EC2, 2 RDS, 3 EBS, 1 NAT gateway, 2 S3 buckets) in `ca-central-1`.
- **Costs:** 30 days of daily FOCUS-style cost rows, plus team budgets and commitments.
- **Operations data:** 14 days of hourly metrics and 31 log events.
- **Security and state:** a security snapshot and Terraform state.

The answer key lists 12 governance findings, 3 incidents and 3 decoys, which look suspicious but are fine. It's
written to `data/answer_key/`, outside the folder the agents read.

## Two sources, same shapes

| Source | What it is | Use it for |
|---|---|---|
| JSON (default) | `data/novapay/*.json` | tests, fast runs, no Docker |
| AWS Emulator (optional) | the same estate, built through real AWS APIs in [AWS Emulator](https://github.com/aws/aws-emulator) on this machine | checking that tools work against AWS-shaped APIs, with no AWS account |

`novapay_agents.aws_env` seeds the emulator and reads it back in the same shapes as the JSON files, so agent
tools can take either source.

| Mock data | Where it lives in the emulator |
|---|---|
| EC2 instances, EBS volumes, security groups, NAT gateway | EC2 API, in one VPC and subnet |
| RDS instances | RDS API |
| S3 buckets: tags, encryption, public access | S3 API: bucket tags, default encryption, public access block, bucket policy |
| Hourly metrics | CloudWatch, custom namespace `NovaPay/Mock` |
| Logs | CloudWatch Logs group `/novapay/app`, one stream per source |
| On-call wiring (emulator only) | SNS topic `novapay-oncall` and one alarm on checkout p95 latency |
| Time shift (see below) | SSM parameter `/novapay/seed/time-shift-hours` |

Costs, budgets, commitments and Terraform state stay in JSON: billing data can't be emulated.

### Measured round trip

AWS Emulator 1.0.338 in Docker (colima) on an Apple M4 laptop, 2026-11-13. The seed is read back and compared
item by item with the JSON (`aws_env check`):

| Check | Matched |
|---|---|
| Inventory resources | 15 / 15 |
| Security groups (rules and attachments) | 3 / 3 |
| S3 buckets (public read, encryption) | 2 / 2 |
| Instance metadata settings (IMDS) | 4 / 4 |
| Metric data points | 10,416 / 10,416 |
| Log events | 31 / 31 |

Seeding took 1.7 s and the check took 0.5 s (wall clock, one run each).

### What failed along the way

- **CloudWatch rejected the first seed.** Like AWS, it only accepts data points from the last two weeks, and the
  mock window is in October. The seed now moves every metric and log timestamp forward by a whole number of hours
  and stores that number in SSM; reading back moves them back. Hour-of-day patterns, like the nightly batch, keep
  their original times when read through `read_back`. Viewed directly in CloudWatch, they're shifted.
- **The first read-back lost every metric point** (0 / 10,416). Hourly buckets start at the query's start time,
  which wasn't on a whole hour, so every timestamp came back 35 minutes off. The query now starts on the hour.
- **AWS doesn't allow security group names starting with `sg-`**, so `sg-web` is the group `novapay-web` with a
  `Name` tag of `sg-web`.
- **One PutLogEvents batch can't span more than 24 hours.** Logs are sent in day-sized batches.

### Modelling choices to know about

- **The mock attaches an EBS volume to an RDS database** (`vol-0c3` to `db-01`), which AWS doesn't allow. In the
  emulator it's an unattached volume tagged `novapay:attached-to=db-01`, and `read_back` reports it as in use.
- **S3 bucket sizes** (used for storage costs) are kept in a `novapay:size-gb` tag.
- **Instances the mock gives no IMDS setting get IMDSv2 required,** so the emulator doesn't add IMDSv1 findings
  that the answer key doesn't have.
- **Instance root volumes** that the emulator creates are left out of the inventory, as in the mock.

### Limits of the emulator

- **It doesn't evaluate IAM.** Nothing here proves least privilege or that a tool is read-only; that has to be
  tested another way.
- **One Region and one account, in memory.** Restart the container before seeding again. It has no reset API.
- **Fidelity varies by service.** AWS publishes conformance scores with each release (`scores.json.gz` on the
  [releases page](https://github.com/aws/aws-emulator/releases)). For v1.0.338, the release used here:

| Service | Scored tests passed, whole service | Test steps that call the operations this repo uses |
|---|---|---|
| EC2 | 770 / 1,731 (44%) | 715 / 882 (81%) |
| RDS | 240 / 482 (50%) | 25 / 31 (81%) |
| S3 | 1,014 / 1,226 (83%) | 550 / 573 (96%) |
| CloudWatch | 645 / 848 (76%) | 224 / 277 (81%) |
| CloudWatch Logs | 480 / 643 (75%) | 253 / 253 (100%) |
| SNS | 239 / 284 (84%) | 39 / 39 (100%) |
| SSM | 108 / 457 (24%) | 44 / 44 (100%) |

  Only single-service tests are counted. The round trip above is what this repo actually relies on.

- **Alarms:** the emulator set the checkout alarm's state to `OK` after the seed. Nothing here tests whether it
  would go into `ALARM` or notify SNS, so don't rely on that.

## How to run

Default mode, no Docker:

```bash
uv run python -m novapay_agents.generate
uv run python -m pytest
```

AWS Emulator mode. Build the image once from AWS's
[Getting started with Docker](https://docs.aws.amazon.com/emulator/latest/userguide/getting-started-docker.html)
Dockerfile (on Apple silicon, use `aws-emulator-aarch64.rpm`), then start it with telemetry off and open only on
this machine:

```bash
docker build -t aws-emulator .        # in an empty folder outside this repo
mkdir -p ~/.aws/emulator
docker run -d --rm --name aws-emulator --memory=2g --memory-swap=2g -p 127.0.0.1:9297:9297 \
  -e HOME="$HOME" -v "$HOME/.aws/emulator:$HOME/.aws/emulator" aws-emulator -p 9297 --telemetry off
```

Then seed, check, and run the round-trip test:

```bash
uv sync --extra aws
source scripts/emulator-env.sh       # run-only keys, CA and a local endpoint; read again after each restart
uv run python -m novapay_agents.aws_env seed
uv run python -m novapay_agents.aws_env check
NOVAPAY_AWS_EMULATOR=1 uv run python -m pytest
```

`aws_env` refuses to run unless `AWS_ENDPOINT_URL` points at `127.0.0.1`, `localhost` or `::1`, so nothing can
reach real AWS. No AWS account is needed.

## Layout

```
src/novapay_agents/generate.py   seeded mock estate and answer key
src/novapay_agents/aws_env.py    optional AWS Emulator mode: seed, read back, check
scripts/emulator-env.sh          points boto3 at the local emulator
data/novapay/                    the estate as JSON (what agents read)
data/answer_key/                 planted problems and decoys (agents never read this)
docs/architecture.svg            the diagram above
```
