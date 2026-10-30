# 10. Reconciliation: after the fix

Proofs for section 10 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). The command in the output shows the script's old
path, `scripts/`; today it is:

```bash
python3 docs/proofs/10-reconciliation/scripts/verify-bindings-written-once.py
```

## Unit tests

Go `fmt-check`, `vet` and `go test ./...` pass, including
`TestHeartbeatSkipsUnchangedSandboxList`, `TestUnregisterNodeKeepsBindings`,
`TestLookupNodeReportsUnknownNodeUnavailable` and
`TestBindingStoreReconcileNodeIgnoresAddressChange`. The Kubernetes part is
unit-tested only, not run on a cluster.

## Output after the fix (2026-10-28)

```text
$ python3 scripts/verify-bindings-written-once.py
scheduler: services/bin/scheduler -config services/config/lima.json
[00:32:10] restarted the scheduler: pid 74589, binding store redis
[00:32:20] created 01a17ebd-e26f-7191-89d0-fd0a9d717004 through the gateway on node-a; Redis: {"node":{"node_id":"node-a","endpoint":"http://127.0.0.1:8000"}}, expires in 31535999 s
[00:32:28] created 01a17ebe-0165-75b1-ae3a-d5efd444952d through the gateway on node-b; Redis: {"node":{"node_id":"node-b","endpoint":"http://127.0.0.1:8001"}}, expires in 31535999 s

== Steady state: 30 s with no restarts and no creates
  heartbeat sandbox lists: applied 0, skipped as unchanged 12
  Redis: 0 scripts (one per list or record call), SET 0, SADD 0, DEL 0
  binding of 01a17ebd-e26f-7191-89d0-fd0a9d717004: expires in 31535992 s, 30 s later in 31535962 s
  PASS: every list was skipped as unchanged
  PASS: no binding was written
  PASS: the binding was not renewed

== Scheduler restart
[00:32:58] restarted the scheduler: pid 74787, binding store redis
[00:32:59] GET 01a17ebd-e26f-7191-89d0-fd0a9d717004 through the gateway: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
  PASS: 01a17ebd-e26f-7191-89d0-fd0a9d717004 found right after the scheduler restart
[00:32:59] GET 01a17ebe-0165-75b1-ae3a-d5efd444952d through the gateway: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
  PASS: 01a17ebe-0165-75b1-ae3a-d5efd444952d found right after the scheduler restart
[00:33:14] 15 s after the restart:
  heartbeat sandbox lists: applied 2, skipped as unchanged 4
  Redis: 2 scripts (one per list or record call), SET 0, SADD 2, DEL 0
  PASS: each node's first list was applied, then skipped
  PASS: no binding was rewritten
  binding of 01a17ebd-e26f-7191-89d0-fd0a9d717004: expires in 31535946 s

== Restart of node-a's server (systemctl restart aenv)
[00:33:14] restarted node-a's server
[00:33:15] GET 01a17ebd-e26f-7191-89d0-fd0a9d717004 through the gateway: HTTP 200 '{"templateID":"docker.io/library/ubuntu@'
  PASS: 01a17ebd-e26f-7191-89d0-fd0a9d717004 found after its node restarted
  heartbeat sandbox lists: applied 1, skipped as unchanged 2
  Redis: 1 scripts (one per list or record call), SET 0, SADD 1, DEL 0
  PASS: node-a's first list after its restart was applied
  PASS: no binding was deleted or rewritten
  binding of 01a17ebd-e26f-7191-89d0-fd0a9d717004: expires in 31535939 s

== A binding that names a node the scheduler does not know
[00:33:21] binding 01a17d00-0000-7000-8000-000000000000 -> {"node": {"node_id": "node-gone", "endpoint": "http://127.0.0.1:8000"}}
[00:33:21] GET 01a17d00-0000-7000-8000-000000000000 through the gateway: HTTP 503 'node unavailable'
  PASS: not sent to the saved address

== Delete through the gateway
[00:33:21] DELETE 01a17ebd-e26f-7191-89d0-fd0a9d717004: HTTP 204; binding still in Redis: True
[00:33:21] GET 01a17ebd-e26f-7191-89d0-fd0a9d717004 through the gateway: HTTP 404 '{"code":404,"message":"sandbox 01a17ebd-' (answered by node-a)
[00:33:21] DELETE 01a17ebe-0165-75b1-ae3a-d5efd444952d: HTTP 204; binding still in Redis: True
[00:33:21] GET 01a17ebe-0165-75b1-ae3a-d5efd444952d through the gateway: HTTP 404 '{"code":404,"message":"sandbox 01a17ebe-' (answered by node-b)
[00:33:25] 4 s later, without restarts: 2 of 2 bindings removed by the nodes' next lists
  PASS: the nodes' next heartbeats removed the bindings of deleted sandboxes

The scheduler keeps running, detached; log: /tmp/verify-bindings-scheduler.log
VERIFIED: bindings were written once and survived both restarts; unchanged lists did not touch Redis, and the next heartbeat removed the bindings of deleted sandboxes
exit=0
```

- **Steady state:** the same 12 lists arrived and were all skipped: no Redis
  command at all (before: 12 SETs), and the binding's time to live just
  counts down.
- **Scheduler restart:** both sandboxes were found right after the restart,
  from Redis. Each node's first list was applied once (reads only, `SET 0`).
- **Node restart:** the node's first list was applied and matched Redis, so
  nothing was deleted or rewritten (`SET 0`, `DEL 0`).
- **Unknown node:** a binding naming `node-gone`, with node-a's live address
  saved in it, got `503 node unavailable`; the gateway did not send the
  request to node-a.
- **Delete:** the nodes' next heartbeats removed both bindings 4 s later,
  with no restart.

## Redis loses its data (2026-10-30)

Skipping unchanged lists had a gap: if Redis lost its data (a restart
without persistence, or a failover to an empty replica), the scheduler kept
skipping the lists it had already applied, so bindings stayed missing until
the 10-minute reapply. Now the scheduler keeps a marker key in Redis
(`agentenv:scheduler:bindings:present`) and checks it about every 5 s with
one `SETNX`; if it is gone, every node's next list is applied again.

### Unit test

`TestHeartbeatRewritesBindingsRedisLost` flushes the test Redis between two
heartbeats with the same list. Before the change it failed:

```text
--- FAIL: TestHeartbeatRewritesBindingsRedisLost (0.18s)
    service_test.go:791: expected the next heartbeat to rewrite the binding Redis lost, got NotFound
```

After it, it passes, with the race detector too, and
`make -C services fmt-check vet` and `go test ./...` in `services/` pass.

### Lab run (2026-10-30 00:35 IST)

[scripts/reproduce-redis-data-loss.py](scripts/reproduce-redis-data-loss.py)
creates one sandbox, then, with the scheduler built before the change and
with the new one, waits until lists are skipped as unchanged, deletes the
scheduler's Redis keys, and polls the sandbox through the gateway:

```text
[00:35:50] created sandbox 01a1890d-ccd4-7662-827d-eb232712b7b6 through the gateway: HTTP 201

== old scheduler (/tmp/scheduler-pre-redis-loss)
[00:36:06] started it 15 s ago; each node's list was applied, then skipped as unchanged
[00:36:06] GET 01a1890d-ccd4-7662-827d-eb232712b7b6: HTTP 200
[00:36:06] Redis lost the scheduler's data: deleted its 3 keys
[00:37:06] GET 01a1890d-ccd4-7662-827d-eb232712b7b6: still HTTP 404 'sandbox assignment not found' 60 s after the loss

== new scheduler (/Users/snutheti/work/AgentENV/services/bin/scheduler)
[00:37:23] started it 15 s ago; each node's list was applied, then skipped as unchanged
[00:37:23] GET 01a1890d-ccd4-7662-827d-eb232712b7b6: HTTP 200
[00:37:23] Redis lost the scheduler's data: deleted its 3 keys
[00:37:28] GET 01a1890d-ccd4-7662-827d-eb232712b7b6: HTTP 200, 5 s after the loss

[00:37:28] cleanup: DELETE 01a1890d-ccd4-7662-827d-eb232712b7b6: HTTP 204; the new scheduler keeps running (log /tmp/lab-redis-loss-scheduler.log)
```

The new scheduler's log:

```text
2026-10-30T00:37:25.999+0530    warn    internal/service.go:386 binding store lost its data; each node's next heartbeat rewrites its bindings
```
