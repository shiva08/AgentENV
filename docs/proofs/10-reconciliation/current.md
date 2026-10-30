# 10. Reconciliation: current behavior (before the fix)

Proofs for section 10 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md). Commands inside recorded outputs show the
scripts' old path, `scripts/`.

## Records expire 30 s after the last heartbeat (2026-10-27 01:39 IST)

Create a real `ubuntu:22.04` sandbox through the gateway, block heartbeats
from its node with a temporary nftables table, and compare
`GET /sandboxes/{id}` through the gateway with the same request sent directly
to the node:

```text
01:39:46 created sandbox 01a179d5-4501-7881-9acd-453cfe9754af through the gateway; it runs on node-a
01:39:46 GET /sandboxes/01a179d5-4501-7881-9acd-453cfe9754af  via gateway: HTTP 200 {"templateID":"docker.io/library/ubuntu@  | directly on node-a: HTTP 200
01:39:46 blocked node-a's heartbeats; node-a and the sandbox keep running
01:40:16 scheduler says: node-a=unhealthy node-b=ready
01:40:16 GET /sandboxes/01a179d5-4501-7881-9acd-453cfe9754af  via gateway: HTTP 404 sandbox assignment not found  | directly on node-a: HTTP 200
01:40:17 removed the block
01:40:26 scheduler says: node-a=ready node-b=ready
01:40:26 GET /sandboxes/01a179d5-4501-7881-9acd-453cfe9754af  via gateway: HTTP 200 {"templateID":"docker.io/library/ubuntu@  | directly on node-a: HTTP 200
01:40:26 delete through the gateway: HTTP 204
```

30 s without heartbeats made a running sandbox look deleted through the
gateway, while node-a still served it.

## Records come from the create, not from heartbeats (2026-10-27 03:32 IST)

Three experiments with real `ubuntu:22.04` sandboxes, all deleted afterwards:

```text
== B: create directly on node-a, bypassing the gateway (heartbeats normal)
03:31:59.671 create directly on node-a: HTTP 201, sandbox 01a17a3c-01f5-7a21-9722-79e6cddf7011
03:31:59.680 GET /sandboxes/01a17a3c-01f5-7a21-9722-79e6cddf7011 through gateway: HTTP 200 '{"templateID":"docker.io/library/ubunt'  (0.0 s after the create)
03:31:59.680 delete through gateway: HTTP 204

== A: create through the gateway while heartbeats from both nodes are blocked
03:32:00.363 blocked heartbeats from both nodes: no heartbeat can report a new sandbox
03:32:07.579 create through gateway: HTTP 201, sandbox 01a17a3c-1f55-7112-9ece-568050f92503
03:32:07.582 GET /sandboxes/01a17a3c-1f55-7112-9ece-568050f92503 through gateway right after: HTTP 200 '{"templateID":"docker.io/library/ubunt'
03:32:07.724 removed the blocks
03:32:07.724 delete through gateway: HTTP 204

== B': create directly on node-a while node-a's heartbeats are blocked
03:32:44.762 blocked node-a's heartbeats
03:32:53.404 create directly on node-a: HTTP 201, sandbox 01a17a3c-cd2c-7f80-9115-d0cf2240417c
03:32:53.418 GET through gateway: HTTP 404 'sandbox assignment not found'
03:32:53.421 GET directly on node-a: HTTP 200
03:32:53.543 removed the block
03:32:53.544 GET through gateway: HTTP 404 'sandbox assignment not found'  (0.0 s after the block was removed)
03:32:54.054 GET through gateway: HTTP 200 '{"templateID":"docker.io/library/ubunt'  (0.5 s after the block was removed)
03:32:54.054 delete directly on node-a: HTTP 204
```

- **A:** with no heartbeat able to report it, the gateway found the new
  sandbox 3 ms after the create returned: the record came from the create.
- **B:** a cold create takes 7 to 9 s here, longer than the 5 s heartbeat
  interval, so a heartbeat had already reported the sandbox while it was
  starting.
- **B':** only a create that bypasses the gateway depends on the heartbeat.

## With fixed node names, the list is not needed once records last (2026-10-27 04:44 IST)

[scripts/verify-bindings-without-heartbeat-lists.py](scripts/verify-bindings-without-heartbeat-lists.py)
runs the same test with two scheduler settings: the old defaults (records
expire 30 s after their last write and live in the scheduler's memory) and
durable ones (`SCHEDULER_BINDING_TTL=24h SCHEDULER_REDIS_ADDR=127.0.0.1:6379`).
Each test creates a sandbox through the gateway, blocks heartbeats from both
nodes so no sandbox list reaches the scheduler, and looks the sandbox up
through the gateway right away, 35 s later and after restarting the
scheduler. Exit status 0 means verified.

```bash
python3 docs/proofs/10-reconciliation/scripts/verify-bindings-without-heartbeat-lists.py
```

Output (exit status 0):

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Node names are fixed in services/config/lima.json, so a sandbox's node never changes after create.
Scheduler output goes to /tmp/verify-bindings-scheduler.log.

== Scheduler with today settings: records expire after 30 s and live in the scheduler's memory
[04:42:31] restarted the scheduler: pid 57141, binding store memory, binding_ttl 30s
[04:42:44] created sandbox 01a17a7c-c68d-7320-9e72-e4bc4fd32348 through the gateway: HTTP 201, on node-a
[04:42:44] blocked heartbeats from both nodes: no sandbox list reaches the scheduler
[04:42:44] GET through the gateway, right after the block: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
[04:43:19] GET through the gateway, 35 s later: HTTP 404 'sandbox assignment not found'
[04:43:19] restarted the scheduler: pid 62739, binding store memory, binding_ttl 30s
[04:43:26] GET through the gateway, after a scheduler restart: HTTP 404 'sandbox assignment not found'
[04:43:26] GET directly on node-a: HTTP 200
[04:43:26] DELETE directly on node-a, since the gateway lost the record: HTTP 204
[04:43:27] removed the heartbeat blocks

== Scheduler with durable settings: records do not expire (binding_ttl 24h) and live in Redis
[04:43:27] restarted the scheduler: pid 62754, binding store redis, binding_ttl 24h
[04:44:11] created sandbox 01a17a7e-194e-7382-9257-4d5ee75f180f through the gateway: HTTP 201, on node-a
[04:44:11] Redis: agentenv:scheduler:bindings:sandbox:01a17a7e-194e-7382-9257-4d5ee75f180f = {"node":{"node_id":"node-a","endpoint":"http://127.0.0.1:8000"}}, expires in 24.0 h
[04:44:11] blocked heartbeats from both nodes: no sandbox list reaches the scheduler
[04:44:11] GET through the gateway, right after the block: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
[04:44:46] GET through the gateway, 35 s later: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
[04:44:46] restarted the scheduler: pid 63166, binding store redis, binding_ttl 24h
[04:44:52] GET through the gateway, after a scheduler restart: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
[04:44:52] GET directly on node-a: HTTP 200
[04:44:52] DELETE through the gateway: HTTP 204
[04:44:52] GET through the gateway after the delete: HTTP 404 '{"code":404,"message":"sandbox 01a17a7e-'  (answered by node-a, because the record still points there)
[04:44:53] removed the heartbeat blocks

[04:44:53] left the scheduler running with today's settings (pid 63183, log /tmp/verify-bindings-scheduler.log)

== Result
  GET through the gateway        today  durable
  right after the block            200      200
  35 s later                       404      200
  after a scheduler restart        404      200
VERIFIED: with fixed node names and no heartbeat sandbox lists, the gateway kept finding the
sandbox once records did not expire and survived a scheduler restart. With today's settings
it lost the record, so today the list is needed only to refresh and rebuild records.
```

## Steady state before the fix (2026-10-28)

[scripts/verify-bindings-written-once.py](scripts/verify-bindings-written-once.py)
creates two sandboxes through the gateway, then counts the heartbeat lists the
scheduler applies or skips as unchanged (metric
`agentenv_scheduler_sandbox_lists_total`) and Redis writes
(`INFO commandstats`). The nodes are unmodified v0.2.4. Here, the committed
scheduler with its 30 s default:

```text
$ python3 scripts/verify-bindings-written-once.py --steady-only --scheduler-bin /tmp/scheduler-head --env SCHEDULER_BINDING_TTL=30s
scheduler: /tmp/scheduler-head -config services/config/lima.json SCHEDULER_BINDING_TTL=30s
[00:31:17] restarted the scheduler: pid 74306, binding store redis
[00:31:32] created 01a17ebd-1dcf-7660-963f-e03a5dcaaf52 through the gateway on node-a; Redis: {"node":{"node_id":"node-a","endpoint":"http://127.0.0.1:8000"}}, expires in 29 s
[00:31:39] created 01a17ebd-43c0-7d83-a5fc-993e0d3adb85 through the gateway on node-b; Redis: {"node":{"node_id":"node-b","endpoint":"http://127.0.0.1:8001"}}, expires in 29 s

== Steady state: 30 s with no restarts and no creates
  sandbox lists: in every heartbeat, always applied (this scheduler does not count them)
  Redis: 12 scripts (one per list or record call), SET 12, SADD 12, DEL 0
  binding of 01a17ebd-1dcf-7660-963f-e03a5dcaaf52: expires in 26 s, 30 s later in 26 s
exit=0
```

Every heartbeat rewrote its node's bindings: 12 SETs in 30 s for 2
sandboxes, and the binding never got older than 30 s.
