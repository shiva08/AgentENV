# 5. Failure detection: after the fix

Proofs for section 5 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps.

## Unit tests

Before the change, the 3 new cases failed:

```text
--- FAIL: TestScheduleSkipsNodesWithoutFreshHeartbeat (0.00s)
    --- FAIL: TestScheduleSkipsNodesWithoutFreshHeartbeat/stale_heartbeat (0.00s)
        service_test.go:306: schedule 1: expected node-a, got node-b
    --- FAIL: TestScheduleSkipsNodesWithoutFreshHeartbeat/unregistered (0.00s)
        service_test.go:306: schedule 1: expected node-a, got node-b
    --- FAIL: TestScheduleSkipsNodesWithoutFreshHeartbeat/never_reported (0.00s)
        service_test.go:306: schedule 1: expected node-a, got node-b
```

After it, all `Schedule` tests pass, and `make -C services fmt-check vet` and
`make -C services test` pass:

```text
--- PASS: TestScheduleReturnsUnavailableWhenRegistryIsEmpty (0.00s)
--- PASS: TestScheduleOnlyConsidersReadyNodes (0.00s)
--- PASS: TestScheduleSkipsNodesWithoutFreshHeartbeat (0.00s)
    --- PASS: TestScheduleSkipsNodesWithoutFreshHeartbeat/stale_heartbeat (0.00s)
    --- PASS: TestScheduleSkipsNodesWithoutFreshHeartbeat/unregistered (0.00s)
    --- PASS: TestScheduleSkipsNodesWithoutFreshHeartbeat/never_reported (0.00s)
--- PASS: TestScheduleFailsWhenNoNodeIsReady (0.00s)
PASS
ok      agentenv/services/scheduler/internal    1.000s
```

## Runs after the fix (2026-10-27 00:32-00:34 IST)

These ran with the first version of the fix, which still used every node when
none was `READY`; with node-a up, that did not apply.

| Run | Case | Step 2: creates sent to node-b | Exit |
| --- | --- | --- | --- |
| 00:32:26 | crash, no arguments | 0 of 4 | 0 |
| 00:32:56 | silence | 0 of 4 | 0 |
| 00:33:59 | clean stop, by hand | 0 of 6 | - |

Crash run, no arguments:

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[00:32:26] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[00:32:27] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode crash)
[00:32:27] killed node-b's server (SIGKILL): it no longer serves or sends heartbeats
[00:32:27] waiting until the scheduler marks node-b unhealthy (no heartbeat for 30 s) ...
[00:32:54] scheduler says: node-a=ready node-b=unhealthy   (27 s after node-b went silent)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[00:32:54] scheduler still says: node-a=ready node-b=unhealthy
  => node-a got 4, node-b got 0

== Step 3: node-b healthy again
[00:32:54] started node-b's server
[00:32:55] scheduler says: node-a=ready node-b=ready   (1 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  => node-a got 2, node-b got 2

== Result
PASS: the scheduler sent no creates to node-b while it was unhealthy.
```

Silence run:

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[00:32:56] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[00:32:56] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode silence)
[00:32:56] blocked node-b's traffic to the scheduler (port 9090): it keeps serving, but its heartbeats fail
[00:32:56] waiting until the scheduler marks node-b unhealthy (no heartbeat for 30 s) ...
[00:33:25] scheduler says: node-a=ready node-b=unhealthy   (29 s after node-b went silent)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[00:33:25] scheduler still says: node-a=ready node-b=unhealthy
  => node-a got 4, node-b got 0

== Step 3: node-b healthy again
[00:33:25] removed the block; node-b retries heartbeats with backoff (up to 60 s)
[00:33:35] scheduler says: node-a=ready node-b=ready   (10 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  => node-a got 2, node-b got 2

== Result
PASS: the scheduler sent no creates to node-b while it was unhealthy.
```

Clean stop, by hand. node-b unregisters, and the creates use body `{}` as
before:

```text
00:33:59 before: node-a=ready node-b=ready
00:34:00 node-b AgentENV service stopped (clean stop, so it unregisters)
00:34:01 scheduler now says: node-a=ready
create 1: HTTP 422  Failed to deserialize the JSON body into
create 2: HTTP 422  Failed to deserialize the JSON body into
create 3: HTTP 422  Failed to deserialize the JSON body into
create 4: HTTP 422  Failed to deserialize the JSON body into
create 5: HTTP 422  Failed to deserialize the JSON body into
create 6: HTTP 422  Failed to deserialize the JSON body into
node-a's create counter went up by 6
00:34:02 after restore: node-a=ready node-b=ready
```

## Runs after the final version (2026-10-27 00:46-00:47 IST)

The crash run with no arguments passed again (exit 0). Its step 2:

```text
== Step 2: node-b stops sending heartbeats (--mode crash)
[00:46:46] killed node-b's server (SIGKILL): it no longer serves or sends heartbeats
[00:46:46] waiting until the scheduler marks node-b unhealthy (no heartbeat for 30 s) ...
[00:47:12] scheduler says: node-a=ready node-b=unhealthy   (26 s after node-b went silent)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[00:47:12] scheduler still says: node-a=ready node-b=unhealthy
  => node-a got 4, node-b got 0
```

No node `ready`, by hand: a temporary nftables table in both VMs rejected
traffic to the scheduler port, so both nodes kept serving but stopped
reporting:

```text
00:47:14 before: node-a=ready node-b=ready
00:47:14 blocked heartbeats from both nodes to the scheduler (port 9090); both keep serving
00:47:43 scheduler now says: node-a=unhealthy node-b=unhealthy
create 1: HTTP 503  no ready nodes: no heartbeat within report_ttl
create 2: HTTP 503  no ready nodes: no heartbeat within report_ttl
00:47:43 removed the block on both nodes
00:47:53 scheduler now says: node-a=ready node-b=ready
create 1: HTTP 422  Failed to deserialize the JSON body into the target type: mi
create 2: HTTP 422  Failed to deserialize the JSON body into the target type: mi
```

With no node `ready`, creates fail with `503` before reaching any node, so a
retry is safe. Section 6 later shortened the message to `no ready nodes`.
