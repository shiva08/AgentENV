# 4. Redis high availability: after the heartbeat fix

Proofs for section 4 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps. Only the heartbeat part is
built; Redis failover is not.

## Unit test

`TestHeartbeatSucceedsWhileTheBindingStoreIsDown` sends a heartbeat while the
binding store fails, then one after it is back. Before the change it failed:

```text
--- FAIL: TestHeartbeatSucceedsWhileTheBindingStoreIsDown (0.00s)
    service_test.go:838: a failed binding write must not fail the heartbeat, got rpc error: code = Unavailable desc = binding store unavailable
```

After it, the heartbeat answers OK, still carries the shared CPU features,
and the next heartbeat writes the binding. All heartbeat tests pass with the
race detector, and `make -C services fmt-check vet` and `go test ./...` in
`services/` pass:

```text
--- PASS: TestHeartbeatRewritesBindingsRedisLost (0.17s)
--- PASS: TestHeartbeatSucceedsWhileTheBindingStoreIsDown (0.00s)
ok      agentenv/services/scheduler/internal    2.264s
```

## Output after the fix (2026-10-30 01:20 IST)

```text
[01:20:12] scheduler and gateway use Redis 127.0.0.1:6380; node-a=ready node-b=ready
[01:20:25] created A = 01a18936-8bc5-77d2-92b2-d7193e05f3bb: HTTP 201; GET A: 200 '{"templateID":"docker.io/library'

== Redis is down
[01:20:25 +  0 s] stopped Redis
[01:20:25 +  0 s] GET A: 503 'binding store unavailable'
[01:20:25 +  0 s] create with an Idempotency-Key: 503 'idempotency store unavailable'
[01:20:33 +  8 s] create B1 without a key: HTTP 201 01a18936-c1df-78c1-92e0-ea60a86befef
[01:20:41 + 15 s] create B2 without a key: HTTP 201 01a18936-dd43-7da2-bc0f-57d47811cf0d
[01:20:51 + 25 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:01 + 36 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:11 + 46 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:21 + 56 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:31 + 66 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:41 + 76 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:21:52 + 86 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:22:02 + 96 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:22:12 +106 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:22:22 +116 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'
[01:22:32 +126 s] node-a=ready node-b=ready         GET A: 503 'binding store unavailable'          create {}: 422 'Failed to deserialize the JSON b'

== Redis is back, empty
[01:22:32 +127 s] started Redis again
  both nodes ready: after 0 s
  GET A answers 200: after 2 s
  GET B1 answers 200: after 2 s
  GET B2 answers 200: after 2 s
[01:22:35 +130 s] cleanup: DELETE 01a18936-8bc5-77d2-92b2-d7193e05f3bb: HTTP 204
[01:22:35 +130 s] cleanup: DELETE 01a18936-c1df-78c1-92e0-ea60a86befef: HTTP 204
[01:22:35 +130 s] cleanup: DELETE 01a18936-dd43-7da2-bc0f-57d47811cf0d: HTTP 204
[01:22:35 +130 s] scheduler log: 47 failed heartbeat writes
[01:22:37 +132 s] restarted the scheduler and the gateway with services/config/lima.json (logs /tmp/lab-*.log)
```

| | Before the fix | After |
| --- | --- | --- |
| Calls on an existing sandbox while Redis is down | `503` | `503`, as before: they need Redis |
| Creates with an `Idempotency-Key` while Redis is down | `503` | `503`, as before |
| Nodes | Flapping from +77 s; both `unhealthy` at +127 s | `ready` throughout |
| Creates without a key | `503 no ready nodes` at +127 s | Reached a node throughout |
| Back to normal after Redis returned | 14 to 21 s | 0 to 2 s |

The 47 failed writes are the nodes' heartbeats every 5 s, each retrying the
write while Redis was down; before the fix, the nodes' growing waits made
only 10.
