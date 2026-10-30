# 7. Atomic capacity check: current behavior (before the fix)

Proofs for section 7 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md).

## How the runs were made

[scripts/reproduce-capacity-overfill.py](scripts/reproduce-capacity-overfill.py)
restarts the scheduler with `max_sandbox_count = 1` added to
[lima.json](../../../services/config/lima.json), then:

1. Sends 4 creates at once to empty nodes.
2. Creates 1 sandbox on each node, waits until the scheduler shows both, and
   sends 1 more create.
3. Sends a create the node rejects (body `{}`, HTTP 422), then 2 creates at
   once. Both must succeed: the rejected create must not keep a slot.

It counts each node's sandboxes by asking the nodes, deletes them between
steps, and finally restarts the scheduler with plain `lima.json`. Exit status
1 means a node went over the limit or a slot was kept; 0 means pass.

```bash
python3 docs/proofs/07-admission-control/scripts/reproduce-capacity-overfill.py                     # services/bin/scheduler
python3 docs/proofs/07-admission-control/scripts/reproduce-capacity-overfill.py --scheduler-bin /tmp/scheduler-pre-capacity
```

## Output before the fix (2026-10-28 02:36 IST)

`/tmp/scheduler-pre-capacity` is the scheduler built before this change.

```text
run d9592e2b1bb4: scheduler /tmp/scheduler-pre-capacity, gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
Each create: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=d9592e2b1bb4.
[02:36:01] restarted the scheduler with node_resource_limit.max_sandbox_count = 1: pid 85424
[02:36:04] scheduler says: node-a 0 running + 0 starting, node-b 0 running + 0 starting

== Step 1: both nodes empty, 4 creates at once
  create 1: HTTP 201, sandbox 01a17f2f-46ea-7e81-b55e-af5e1a560522 on node-b
  create 2: HTTP 201, sandbox 01a17f2f-46cb-7c52-9414-02d5869526ae on node-a
  create 3: HTTP 201, sandbox 01a17f2f-4736-7cd3-bad8-c3190b2770c4 on node-b
  create 4: HTTP 201, sandbox 01a17f2f-46db-7d70-92a9-c6c44d823487 on node-a
  => node-a holds 2, node-b holds 2 (limit 1 per node)
[02:36:18] deleted 4 sandbox(es) directly on the nodes
[02:36:22] scheduler says both nodes are empty (3.5 s later)

== Step 2: each node holds 1 sandbox, then 1 more create
  create 1: HTTP 201, sandbox 01a17f2f-8c6b-75b0-9ce0-8cdd8542058f on node-a
  create 2: HTTP 201, sandbox 01a17f2f-aa4e-7aa2-8600-f593848479eb on node-b
[02:36:39] scheduler says: node-a 1 running + 0 starting, node-b 1 running + 0 starting (2.5 s later)
  create 3: HTTP 201, sandbox 01a17f2f-d01c-7953-a0cc-31d0602d7ce2 on node-a
  => node-a holds 2, node-b holds 1 (limit 1 per node)
[02:36:47] deleted 3 sandbox(es) directly on the nodes
[02:36:52] scheduler says both nodes are empty (4.6 s later)

== Step 3: a create the node rejects, then 2 creates at once
  create with body {}: HTTP 422 'Failed to deserialize the JSON body into'
  create 1: HTTP 201, sandbox 01a17f30-011b-7fd2-a762-e7cae740bc41 on node-b
  create 2: HTTP 201, sandbox 01a17f30-02d9-72e0-b9e9-bb5348e81439 on node-a
  => node-a holds 1, node-b holds 1 (limit 1 per node)

[02:37:00] cleanup: deleted 2 sandbox(es)
[02:37:00] restarted the scheduler with services/config/lima.json alone: pid 85707, log /tmp/verify-bindings-scheduler.log

== Result
BUG REPRODUCED: 4 creates at once left node-a 2/1, node-b 2/1; 1 create on full nodes left node-a 2/1.
```

Step 1 shows the parallel creates all getting in. Step 2 shows the off-by-one:
the scheduler already showed node-a at 1 of 1 and still sent it a create.
Step 3 passes before the fix; it guards the fix against keeping slots for
failed creates. An earlier run at 02:15 gave the same result.
