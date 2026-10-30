# 11. Partial results: current behavior (before the fix)

Proofs for section 11 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md).

## How the run was made

[scripts/reproduce-dead-node-breaks-list.py](scripts/reproduce-dead-node-breaks-list.py)
needs only the Python standard library and `limactl`.

```bash
python3 docs/proofs/11-partial-results/scripts/reproduce-dead-node-breaks-list.py
```

- **Setup:** create sandboxes through the gateway until each node holds one
  (ubuntu:22.04, 1 vCPU, 512 MiB, labelled `repro-run=<run>`). Which node
  holds each one is read from each node's own API.
- **Step 1:** both nodes up; list this run's sandboxes through the gateway,
  with `GET /v2/sandboxes` (running and paused) and `GET /sandboxes` (running
  only).
- **Step 2:** stop node-b's server (`systemctl stop aenv`; it pauses its
  sandbox to its own disk first), then list again. node-a's sandbox must
  still be listed, and the reply must name node-b in
  `x-agentenv-unavailable-nodes`.
- **Step 3:** start node-b again, wait until the scheduler says it is
  `ready`, and list again: all of this run's sandboxes must be back. `GET
  /sandboxes` no longer shows node-b's sandbox, because it is now paused.

Exit status: 1 = bug reproduced, 0 = pass, 2 = setup error. The API key comes
from `AENV_API_KEY`, or from node-a's VM when that is unset. node-b is started
again and the sandboxes are deleted on exit, including after Ctrl-C.

## Output before the fix (2026-10-29 00:39 IST)

The gateway built from `local/dev` before this fix (kept as
`/tmp/gateway-pre-partial-list`). Exit code 1:

```text
run acec737238a3: gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
Each sandbox: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=acec737238a3.
[00:39:09] both nodes ready (0.0 s)

== Setup: one sandbox on each node
  create 1: HTTP 201, sandbox 01a183ea-9b5b-78b0-ac36-055b085fafc0 on node-b
  create 2: HTTP 201, sandbox 01a183ea-b8e5-7312-b1f4-820c67445c6e on node-a

== Step 1: both nodes up
  GET /v2/sandboxes: HTTP 200 in 0.00 s; sandboxes: ...67445c6e on node-a, ...085fafc0 on node-b
  GET /sandboxes: HTTP 200 in 0.00 s; sandboxes: ...67445c6e on node-a, ...085fafc0 on node-b

== Step 2: node-b's server stopped
[00:39:25] systemctl stop aenv on node-b: done
  GET /v2/sandboxes: HTTP 502 in 0.01 s; sandboxes: 'cluster list unavailable'
  GET /sandboxes: HTTP 502 in 0.00 s; sandboxes: 'cluster list unavailable'

== Step 3: node-b started again
[00:39:25] node-b ready again (0.5 s after the start)
  GET /v2/sandboxes: HTTP 200 in 0.00 s; sandboxes: ...67445c6e on node-a, ...085fafc0 on node-b
  GET /sandboxes: HTTP 200 in 0.00 s; sandboxes: ...67445c6e on node-a

[00:39:26] cleanup: deleted 2 sandbox(es) directly on the nodes

BUG REPRODUCED: GET /v2/sandboxes answered HTTP 502 while only node-b was down; GET /sandboxes answered HTTP 502 while only node-b was down
```

A first run a minute earlier (00:38, run `ad2084ee8be2`) gave the same `502`s;
it printed the first 8 characters of each sandbox ID, which are the same for
sandboxes created close together, so the script now prints the last 8.
