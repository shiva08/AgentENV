# 4. Redis high availability: before the fix

Proofs for section 4 of [proposal.md](../../proposal.md). Reproduced on
2026-10-30. After the heartbeat fix: [post-fix.md](post-fix.md).

```bash
python3 docs/proofs/04-redis-high-availability/scripts/reproduce-redis-outage.py
```

The script starts its own Redis on `127.0.0.1:6380`, so the lab's Redis is not
touched, and restarts the scheduler and the gateway with a copy of
`services/config/lima.json` that points both at it. It creates sandbox A,
stops Redis, creates B1 and B2 (round-robin puts one on each node, so both
nodes' sandbox lists change), and watches for 2 minutes. A create with body
`{}` makes nothing: the node that gets it answers `422`. Then it starts Redis
again, empty, as after a restart without persistence.

## Output (2026-10-30 00:56 IST)

```text
[00:56:49] scheduler and gateway use Redis 127.0.0.1:6380; node-a=ready node-b=ready
[00:57:02] created A = 01a18921-2153-7320-8f2f-6ab93250e2b1: HTTP 201; GET A: 200 '{"templateID":"docker.io/library'

== Redis is down
[00:57:02 +  0 s] stopped Redis
[00:57:02 +  0 s] GET A: 503 'binding store unavailable'
[00:57:02 +  0 s] create with an Idempotency-Key: 503 'idempotency store unavailable'
[00:57:11 +  9 s] create B1 without a key: HTTP 201 01a18921-5868-7331-ad72-1e5b22dd6275
[00:57:19 + 16 s] create B2 without a key: HTTP 201 01a18921-76ec-7bd3-8224-2fcfbb006c38
[00:57:29 + 26 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:57:39 + 36 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:57:49 + 46 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:57:59 + 57 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:09 + 67 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:19 + 77 s] node-a=ready node-b=unhealthy     GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:29 + 87 s] node-a=unhealthy node-b=ready     GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:39 + 97 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:49 +107 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:58:59 +117 s] node-a=ready node-b=unhealthy     GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[00:59:10 +127 s] node-a=unhealthy node-b=unhealthy GET A: 503 'binding store unavailable'          create {}: 503 'no ready nodes'

== Redis is back, empty
[00:59:10 +127 s] started Redis again
  both nodes ready: after 21 s
  GET A answers 200: after 21 s
  GET B1 answers 200: after 14 s
  GET B2 answers 200: after 21 s
[00:59:32 +150 s] cleanup: DELETE 01a18921-2153-7320-8f2f-6ab93250e2b1: HTTP 204
[00:59:32 +150 s] cleanup: DELETE 01a18921-5868-7331-ad72-1e5b22dd6275: HTTP 204
[00:59:32 +150 s] cleanup: DELETE 01a18921-76ec-7bd3-8224-2fcfbb006c38: HTTP 204
[00:59:32 +150 s] scheduler log: 20 failed heartbeat writes
[00:59:34 +152 s] restarted the scheduler and the gateway with services/config/lima.json (logs /tmp/lab-*.log)
```

The "20 failed heartbeat writes" count both this run and a first run 3
minutes earlier (the log file was shared then; 10 each). The first run gave
the same picture: both nodes `unhealthy` and `503 no ready nodes` at
+128 s.

The scheduler's log: each heartbeat that carried a changed list failed, so
the node waited longer before the next one:

```text
2026-10-30T00:53:47.679+0530    warn    internal/service.go:335 scheduler heartbeat binding reconcile failed    {"node_id": "node-b", "error": "redis reconcile node bindings: dial tcp 127.0.0.1:6380: connect: connection refused"}
2026-10-30T00:53:52.874+0530    warn    internal/service.go:335 scheduler heartbeat binding reconcile failed    {"node_id": "node-b", "error": "redis reconcile node bindings: dial tcp 127.0.0.1:6380: connect: connection refused"}
2026-10-30T00:53:59.660+0530    warn    internal/service.go:335 scheduler heartbeat binding reconcile failed    {"node_id": "node-a", "error": "redis reconcile node bindings: dial tcp 127.0.0.1:6380: connect: connection refused"}
```

## What it shows

- **At once:** every call on an existing sandbox fails with
  `503 binding store unavailable`, and every create with an
  `Idempotency-Key` with `503 idempotency store unavailable`. Creates without
  a key still work: the scheduler places them from memory, and the gateway
  only logs the failed binding write.
- **After about a minute:** a node whose list changed gets an error for each
  heartbeat and waits 5, 10, 20, 40, then 60 s before the next one, so its
  heartbeats end up more than 30 s apart and it is marked `unhealthy` for
  part of each wait. From +77 s the nodes flapped; at +127 s both were down
  and every create got `503 no ready nodes`.
- **After Redis came back:** 14 to 21 s until each sandbox answered and both
  nodes were `ready`, since each node first waited out its retry delay. The
  bindings came back by themselves (section 10's data-loss check).
