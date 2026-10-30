# 6. Retry: after the fix

Proofs for section 6 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps.

## Unit tests: retry on a refused connection

Before the change, the new tests failed:

```text
--- FAIL: TestScheduleSkipsExcludedNodes (0.00s)
    service_test.go:343: schedule 0: expected node-b, got node-a
--- FAIL: TestScheduledCreateRetriesOnAnotherNodeWhenConnectionRefused (0.00s)
    server_test.go:2728: got 502: upstream unavailable
        , want 201
--- FAIL: TestScheduledCreateRetriesOnlyOnce (0.00s)
    server_test.go:2790: scheduled 1 times, want 2
```

`TestScheduledCreateIsNotRetriedOnceTheNodeMayHaveIt` passed before and after,
since the old gateway never retried. After the change all of them pass, and
`make -C services fmt-check vet`, `go vet ./...` and `go test ./...` in
`services/` pass:

```text
--- PASS: TestScheduleSkipsExcludedNodes (0.00s)
ok      agentenv/services/scheduler/internal    0.933s
--- PASS: TestScheduledCreateRetriesOnAnotherNodeWhenConnectionRefused (0.00s)
--- PASS: TestScheduledCreateIsNotRetriedOnceTheNodeMayHaveIt (0.00s)
--- PASS: TestScheduledCreateRetriesOnlyOnce (0.00s)
ok      agentenv/services/gateway/internal      1.462s
```

## Output after the fix (2026-10-27 02:57 IST)

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[02:57:43] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[02:57:43] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode crash-now)
[02:57:43] killed node-b's server (SIGKILL): it no longer serves or sends heartbeats
[02:57:43] waiting until node-b's port refuses connections ...
[02:57:45] node-b's port refuses connections, but scheduler still says: node-a=ready node-b=ready   (1 s after the kill)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[02:57:45] scheduler still says: node-a=ready node-b=ready
  => node-a got 4, node-b got 0

== Step 3: node-b healthy again
[02:57:45] started node-b's server
[02:57:45] scheduler says: node-a=ready node-b=ready   (1 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Result
PASS: every create reached a running node, although the scheduler still listed node-b as ready.
```

The gateway's log shows the retries:

```text
2026-10-27T02:57:45.150+0530    warn    internal/server.go:481  node refused the connection, trying another node        {"error": "dial tcp 127.0.0.1:8001: connect: connection refused", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-27T02:57:45.157+0530    warn    internal/server.go:481  node refused the connection, trying another node        {"error": "dial tcp 127.0.0.1:8001: connect: connection refused", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-27T02:57:45.163+0530    warn    internal/server.go:481  node refused the connection, trying another node        {"error": "dial tcp 127.0.0.1:8001: connect: connection refused", "node": "node-b", "path": "/sandboxes-cold"}
```

The scheduler picked node-b for 3 of the 4 creates, not 2: each retry's
`Schedule` call also takes a round-robin turn, which shifts the order. Each of
those creates reached node-a within milliseconds. The `crash` and `silence`
modes still pass (exit 0) with the new build.

## Unit tests: startup grace

Before the change the new test did not compile (`undefined:
WithStartupGrace`). After it, all `Schedule` tests pass, and
`make -C services fmt-check vet` and `go test ./...` in `services/` pass:

```text
--- PASS: TestScheduleReturnsUnavailableWhenRegistryIsEmpty (0.00s)
--- PASS: TestScheduleOnlyConsidersReadyNodes (0.00s)
--- PASS: TestScheduleSkipsNodesWithoutFreshHeartbeat (0.00s)
--- PASS: TestScheduleFailsWhenNoNodeIsReady (0.00s)
--- PASS: TestScheduleSkipsExcludedNodes (0.00s)
--- PASS: TestScheduleUsesUnreportedNodesDuringStartupGrace (0.00s)
ok      agentenv/services/scheduler/internal    0.936s
```

## Scheduler restarts with the startup grace (2026-10-27 03:12 and 03:13 IST)

Same measurement as in [current.md](current.md). First restart, right after
the rebuild (the new binary took 1.4 s to start: stopped 03:12:13.48,
listening 03:12:14.89):

```text
03:12:06.098 HTTP 422 Failed to deserialize the JSON body into the target type: mi
03:12:13.521 HTTP 503 connection error: desc = "transport: Error while dialing: di
03:12:16.188 HTTP 422 Failed to deserialize the JSON body into the target type: mi
```

Second restart (stopped 03:13:10.51, listening 03:13:10.86):

```text
03:13:04.087 HTTP 422 Failed to deserialize the JSON body into the target type: mi
03:13:10.684 HTTP 503 connection error: desc = "transport: Error while dialing: di
03:13:11.708 HTTP 422 Failed to deserialize the JSON body into the target type: mi
```

The `no ready nodes` phase is gone: creates failed for 1.0 s in the second
run, against 3.5 s before.

## Connect timeouts: a machine that is off (2026-10-30)

A machine that lost power does not refuse connections; they time out. The
first version retried only some of these: Go reports a connect timeout either
as a plain timeout or as `context.DeadlineExceeded`, depending on which timer
fires first, and the gateway checked for `context.DeadlineExceeded` first and
answered `504`. The gateway now checks for a failed connect first.

### Unit test

Before the change, the new test failed:

```text
--- FAIL: TestScheduledCreateRetriesOnAnotherNodeWhenTheConnectTimesOut (0.00s)
    server_test.go:3007: got 504: upstream timeout
        , want 201
```

After it, all gateway tests pass, and `make -C services fmt-check vet` and
`go test ./...` in `services/` pass:

```text
--- PASS: TestScheduledCreateRetriesOnAnotherNodeWhenConnectionRefused (0.00s)
--- PASS: TestScheduledCreateRetriesOnAnotherNodeWhenTheConnectTimesOut (0.00s)
--- PASS: TestScheduledCreateReleasesTheRefusingNodesReservation (0.00s)
ok      agentenv/services/gateway/internal      0.824s
```

### Lab run (2026-10-30 00:33 IST)

[scripts/reproduce-connect-timeout.py](scripts/reproduce-connect-timeout.py)
gives node-b the address `10.255.255.1:8001` in a temporary scheduler config,
where connects time out, while node-b keeps sending heartbeats, so it stays
`ready`. Two extra gateways, the build before the change and the new one,
each get 4 creates with body `{}` (the node answers `422`, so nothing is
created):

```text
[00:33:22] scheduler config: node-b's address is http://10.255.255.1:8001, where connects time out; node-b keeps sending heartbeats

== old gateway (/tmp/gateway-pre-connect-timeout), http://127.0.0.1:8092
[00:33:27] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 after 0.0 s  'Failed to deserialize the JSON body into the'
  create 2: HTTP 422 after 2.0 s  'Failed to deserialize the JSON body into the'
  create 3: HTTP 422 after 2.0 s  'Failed to deserialize the JSON body into the'
  create 4: HTTP 504 after 2.0 s  'upstream timeout'

== new gateway (/Users/snutheti/work/AgentENV/services/bin/gateway), http://127.0.0.1:8093
[00:33:34] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 after 0.0 s  'Failed to deserialize the JSON body into the'
  create 2: HTTP 422 after 2.0 s  'Failed to deserialize the JSON body into the'
  create 3: HTTP 422 after 2.0 s  'Failed to deserialize the JSON body into the'
  create 4: HTTP 422 after 2.0 s  'Failed to deserialize the JSON body into the'
```

The old gateway's log: two timeouts retried, the third answered `504`:

```text
2026-10-30T00:33:29.384+0530    warn    internal/server.go:502  node refused the connection, trying another node        {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-30T00:33:31.390+0530    warn    internal/server.go:502  node refused the connection, trying another node        {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-30T00:33:33.400+0530    warn    internal/server.go:482  proxy request timed out {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold", "target": "http://10.255.255.1:8001/sandboxes-cold"}
```

The new gateway's log: all three retried:

```text
2026-10-30T00:33:36.429+0530    warn    internal/server.go:485  could not connect to the node, trying another node      {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-30T00:33:38.434+0530    warn    internal/server.go:485  could not connect to the node, trying another node      {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold"}
2026-10-30T00:33:40.439+0530    warn    internal/server.go:485  could not connect to the node, trying another node      {"error": "dial tcp 10.255.255.1:8001: i/o timeout", "node": "node-b", "path": "/sandboxes-cold"}
```

Each create sent to node-b costs the 2 s connect timeout before the retry.
