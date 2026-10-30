# 2. Restore: current behavior

Proofs for section 2 of [proposal.md](../../proposal.md). Not fixed yet, so there is
no post-fix file.

## How the runs were made

[scripts/reproduce-lost-node-sandboxes.py](scripts/reproduce-lost-node-sandboxes.py)
needs only the Python standard library and `limactl`.

```bash
python3 docs/proofs/02-restore/scripts/reproduce-lost-node-sandboxes.py               # watches 120 s
python3 docs/proofs/02-restore/scripts/reproduce-lost-node-sandboxes.py --watch 300
```

1. Through the gateway, create two sandboxes on node-b: A and B. Take a
   snapshot S of A and pause B. A keeps running.
2. Power off node-b's VM (`limactl stop --force agentenv-2`). Every process
   and microVM on that machine stops at once, as in a crash.
3. For 120 s, every 30 s, record node-b's status in the scheduler, the
   gateway's answers for A and B, the sandboxes on node-a, and the
   unhealthy-node gauge. A relaunch would appear as a new sandbox on node-a.
4. Act as the agent: look up A's snapshots through the gateway and launch S.
5. Start node-b's VM again (`limactl start`) and ask the gateway for A and B.

Exit status: 1 = nothing relaunched A or B (bug reproduced), 0 = something
did, 2 = setup error. The script deletes its sandboxes and S at the end, and
starts node-b's VM again if it stops early.

- **Run 1:** each node keeps its snapshot repository on its own disk
  (`repository_backend = "posix_fs"`, the default).
- **Run 2:** both nodes share one repository on an S3 server on the Mac
  ("Setup for run 2" below), switched back afterwards.

## Run 1: each node's own repository (2026-10-28 04:01 IST)

```text
run 0472d10edc3e: gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
snapshot repository: node-a posix_fs, node-b posix_fs (repository_backend in /var/lib/aenv/config/config.toml)
Each sandbox: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=0472d10edc3e.

== Step 1: two sandboxes on node-b: A keeps running and gets a snapshot, B is paused
  create 1: sandbox 01a17f7d-7217-7240-9db4-d825ec73323b on node-b
  create 2: sandbox 01a17f7d-910f-7d71-a009-70563f60efaa on node-a, deleted
  create 3: sandbox 01a17f7d-af95-7df2-b186-4c13117ea3bf on node-b
[04:01:52] A = 01a17f7d-7217-7240-9db4-d825ec73323b: snapshot S = 01a17f7d-c80f-7ca0-b200-095b85332f0a (HTTP 201)
[04:01:52] B = 01a17f7d-af95-7df2-b186-4c13117ea3bf: paused (HTTP 204)
[04:01:52] S is visible on: node-b (GET /snapshots/S on each node)
[04:01:52] snapshots of B on node-b: 0 (GET /snapshots?sandboxID=B; the pause made none)
[04:01:52] GET /sandboxes/A through the gateway: HTTP 200, state running
[04:01:52] GET /sandboxes/B through the gateway: HTTP 200, state paused

== Step 2: node-b's machine dies
[04:01:52] powered off VM agentenv-2 (limactl stop --force): every process and VM on it is gone

== Step 3: watch for 120 s
  +  0 s  node-b: ready     | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 0
  + 30 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 0
  + 60 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
  + 90 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
  +120 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
[04:03:52] resume B through the gateway: HTTP 502 'upstream unavailable'
[04:03:52] lines the scheduler wrote to /tmp/verify-bindings-scheduler.log since node-b died: 0

== Step 4: the agent relaunches A from S by hand
[04:03:52] GET /snapshots?sandboxID=A through the gateway: HTTP 200, 0 snapshot(s)
[04:03:52] GET /snapshots?sandboxID=B through the gateway: HTTP 200, 0 snapshot(s)
[04:03:52] POST /sandboxes with templateID S: HTTP 400 '{"code":400,"message":"template 01a17f7d-c80f-7ca0-b200-095b85332f0a not found"}'

== Step 5: node-b's machine comes back
[04:04:03] started VM agentenv-2; node-b serves again and the scheduler says ready (0 s after it answered; 131 s after it died)
[04:04:03] GET /sandboxes/A through the gateway: HTTP 404, sandbox assignment not found
[04:04:03] GET /sandboxes/B through the gateway: HTTP 200, state paused

[04:04:03] cleanup: delete sandbox 01a17f7d-af95-7df2-b186-4c13117ea3bf on node-b: HTTP 204
[04:04:03] cleanup: delete snapshot 01a17f7d-c80f-7ca0-b200-095b85332f0a on node-b: HTTP 204

== Result
REPRODUCED: for 120 s after node-b died, nothing relaunched A or B, and the gateway answered every request for them with an error. The only signs were node-b's status (unhealthy after 27 s) and the scheduler's unhealthy-node gauge.
The agent's own relaunch from S failed: node-a could not find S.
```

## Run 2: shared repository (2026-10-28 04:05 IST)

```text
run c67950f946f4: gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
snapshot repository: node-a oss, node-b oss (repository_backend in /var/lib/aenv/config/config.toml)
Each sandbox: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=c67950f946f4.

== Step 1: two sandboxes on node-b: A keeps running and gets a snapshot, B is paused
  create 1: sandbox 01a17f80-d3a8-7802-a61f-dae9882a56e5 on node-b
  create 2: sandbox 01a17f80-f217-78d3-90cc-7979a4a4f344 on node-a, deleted
  create 3: sandbox 01a17f81-1421-7e03-a747-2002069e4e80 on node-b
[04:05:34] A = 01a17f80-d3a8-7802-a61f-dae9882a56e5: snapshot S = 01a17f81-2783-7482-b02a-d3649648023b (HTTP 201)
[04:05:34] B = 01a17f81-1421-7e03-a747-2002069e4e80: paused (HTTP 204)
[04:05:34] S is visible on: node-a, node-b (GET /snapshots/S on each node)
[04:05:34] snapshots of B on node-b: 0 (GET /snapshots?sandboxID=B; the pause made none)
[04:05:34] GET /sandboxes/A through the gateway: HTTP 200, state running
[04:05:34] GET /sandboxes/B through the gateway: HTTP 200, state paused

== Step 2: node-b's machine dies
[04:05:34] powered off VM agentenv-2 (limactl stop --force): every process and VM on it is gone

== Step 3: watch for 120 s
  +  0 s  node-b: ready     | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 0
  + 30 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
  + 60 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
  + 90 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
  +120 s  node-b: unhealthy | gateway: A 502 'upstream unavailable', B 502 | node-a holds 0 sandbox(es) | unhealthy-node gauge 1
[04:07:34] resume B through the gateway: HTTP 502 'upstream unavailable'
[04:07:34] lines the scheduler wrote to /tmp/verify-bindings-scheduler.log since node-b died: 0

== Step 4: the agent relaunches A from S by hand
[04:07:34] GET /snapshots?sandboxID=A through the gateway: HTTP 200, 1 snapshot(s): 01a17f81-2783-7482-b02a-d3649648023b
[04:07:34] GET /snapshots?sandboxID=B through the gateway: HTTP 200, 0 snapshot(s)
[04:07:36] POST /sandboxes with templateID S: HTTP 201, new sandbox 01a17f83-0133-7450-a915-03ea8ead251a on node-a (A was 01a17f80-d3a8-7802-a61f-dae9882a56e5)

== Step 5: node-b's machine comes back
[04:07:46] started VM agentenv-2; node-b serves again and the scheduler says ready (0 s after it answered; 132 s after it died)
[04:07:46] GET /sandboxes/A through the gateway: HTTP 404, sandbox assignment not found
[04:07:46] GET /sandboxes/B through the gateway: HTTP 200, state paused

[04:07:46] cleanup: delete sandbox 01a17f83-0133-7450-a915-03ea8ead251a on node-a: HTTP 204
[04:07:46] cleanup: delete sandbox 01a17f81-1421-7e03-a747-2002069e4e80 on node-b: HTTP 204
[04:07:46] cleanup: delete snapshot 01a17f81-2783-7482-b02a-d3649648023b on node-a: HTTP 204

== Result
REPRODUCED: for 120 s after node-b died, nothing relaunched A or B, and the gateway answered every request for them with an error. The only signs were node-b's status (unhealthy after 29 s) and the scheduler's unhealthy-node gauge.
The agent's own relaunch from S worked: a new sandbox with a new ID.
```

## What the runs show

| | Run 1: own repository per node | Run 2: shared repository |
| --- | --- | --- |
| A or B relaunched within 120 s | No | No |
| Gateway's answer for A and B, at +0 s and at +120 s | `502 upstream unavailable` | Same |
| Visible signs | node-b `unhealthy` after 27 s; gauge 0 → 1 | After 29 s; gauge 0 → 1 |
| Scheduler log lines while node-b was down | 0 | 0 |
| S visible on | node-b only | node-a and node-b |
| Snapshots of paused B | 0 | 0 |
| Agent looks up A's snapshots after node-b died | 0 found | S found |
| Agent launches S | `400 template ... not found` | `201`: a new sandbox on node-a, with a new ID |
| node-b back (131 s and 132 s after it died): A | `404 sandbox assignment not found` | Same |
| node-b back: B | `200`, paused, same ID | Same |

The unhealthy-node gauge is refreshed every 15 s
([main.go](../../../services/scheduler/cmd/main.go#L81)), so in run 1 it still
showed 0 at +30 s.

## Setup for run 2

A local S3 server (Silo, the MinIO fork the repository's own tests use, see
[minio.rs](../../../crates/test-support/src/minio.rs)) with a bucket for the
snapshots:

```bash
docker run -d --name aenv-silo -p 127.0.0.1:9000:9000 \
  -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
  pgsty/silo:RELEASE.2026-09-16T00-00-00Z server /data
curl --aws-sigv4 'aws:amz:us-east-1:s3' --user minioadmin:minioadmin \
  -X PUT http://127.0.0.1:9000/aenv-snapshots
```

In both VMs: back up `/var/lib/aenv/config/config.toml`, change
`repository_backend` under `[snapshot]` to `"oss"`, add the section below, and
run `sudo systemctl restart aenv`.

```toml
[backend.oss]
endpoint = "http://host.lima.internal:9000"
bucket = "aenv-snapshots"
region = "us-east-1"
access_key_id = "minioadmin"
access_key_secret = "minioadmin"
```

Undo, done after run 2: restore the backup in both VMs, restart `aenv`, and
run `docker rm -f aenv-silo`. The lab is back on `posix_fs`.
