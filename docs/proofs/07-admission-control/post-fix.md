# 7. Atomic capacity check: after the fix

Proofs for section 7 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps.

## Unit tests

The new tests use the new API, so they do not compile against the old code.
Instead, with the reservation step turned off for one run, they fail:

```text
--- FAIL: TestScheduleReservesCapacityForCreatesArrivingTogether (0.00s)
    service_test.go:455: expected one create per node and 18 refused, got placed
 map[node-a:10 node-b:10], refused 0
--- FAIL: TestReleaseReservationFreesTheSlot (0.00s)
    service_test.go:471: expected unavailable, no node has capacity, got <nil>
--- FAIL: TestHeartbeatListingTheSandboxEndsItsReservation (0.00s)
    service_test.go:525: expected unavailable, no node has capacity, got <nil>
```

With the fix, all pass, also 3 times in a row with the race detector
(`go test -race -count=3`). `make -C services fmt-check vet`, `go vet ./...`
and `go test ./...` in `services/` pass:

```text
--- PASS: TestFilterCountsTheCreateAndReservedCreates (0.00s)
--- PASS: TestFilterCountsTheCreatesSize (0.00s)
--- PASS: TestScheduleReservesCapacityForCreatesArrivingTogether (0.00s)
--- PASS: TestReleaseReservationFreesTheSlot (0.00s)
--- PASS: TestHeartbeatListingTheSandboxEndsItsReservation (0.00s)
--- PASS: TestReservationEndsWhenAHeartbeatListedTheSandboxBeforeItsAssignment (0.00s)
--- PASS: TestReservationEndsWhenTheSandboxIsGoneBeforeAnyHeartbeatListsIt (0.00s)
--- PASS: TestReservationsExpire (0.00s)
--- PASS: TestScheduleReservesOnlyForCreatesWithLimits (0.00s)
--- PASS: TestScheduledCreateHandsItsReservationToRecordAssignment (0.00s)
--- PASS: TestScheduledCreateReleasesItsReservationWhenTheNodeRejectsIt (0.00s)
--- PASS: TestScheduledCreateReleasesTheRefusingNodesReservation (0.00s)
--- PASS: TestScheduledCreateKeepsItsReservationWhenTheNodeMayHaveIt (0.00s)
```

Rust: in VM `agentenv`, `cargo check -p agentenv --lib` passes with the new
`scheduler.proto` swapped into the VM's source copy (v0.2.4, not this branch).

## A leak found by the first run after the fix (2026-10-28 02:29 IST)

The first live run after the fix passed step 1 and then failed step 2 on
empty nodes:

```text
== Step 2: each node holds 1 sandbox, then 1 more create
  create 1: HTTP 503 'no node has capacity'
  create 2: HTTP 503 'no node has capacity'
```

The 2 sandboxes from step 1 still held their slots after being deleted: the
only heartbeat that listed each one arrived before the gateway's
`RecordAssignment`, and the sandbox was deleted before the next one. The fix
for this (in [proposal.md](../../proposal.md), section 7) has two tests that failed
before it:

```text
--- FAIL: TestReservationEndsWhenAHeartbeatListedTheSandboxBeforeItsAssignment (0.00s)
    service_test.go:558: the node is empty, got rpc error: code = Unavailable desc = no node has capacity
--- FAIL: TestReservationEndsWhenTheSandboxIsGoneBeforeAnyHeartbeatListsIt (0.00s)
    service_test.go:571: two heartbeats without sbx-1 mean it is gone, got rpc error: code = Unavailable desc = no node has capacity
```

## Output after the fix (2026-10-28 02:37 IST)

```text
run 15a1b98ed138: scheduler services/bin/scheduler, gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
Each create: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=15a1b98ed138.
[02:37:07] restarted the scheduler with node_resource_limit.max_sandbox_count = 1: pid 85923
[02:37:12] scheduler says: node-a 0 running + 0 starting, node-b 0 running + 0 starting

== Step 1: both nodes empty, 4 creates at once
  create 1: HTTP 503 'no node has capacity'
  create 2: HTTP 201, sandbox 01a17f30-5010-7db1-98dc-c1b18789e0dd on node-b
  create 3: HTTP 201, sandbox 01a17f30-51b1-7d72-9146-3247bee93436 on node-a
  create 4: HTTP 503 'no node has capacity'
  => node-a holds 1, node-b holds 1 (limit 1 per node)
[02:37:20] deleted 2 sandbox(es) directly on the nodes
[02:37:24] scheduler says both nodes are empty (3.5 s later)

== Step 2: each node holds 1 sandbox, then 1 more create
  create 1: HTTP 201, sandbox 01a17f30-7e53-7211-8408-9e938a96e175 on node-a
  create 2: HTTP 201, sandbox 01a17f30-98a4-7ad3-a9a8-8268b9d4abd6 on node-b
[02:37:39] scheduler says: node-a 1 running + 0 starting, node-b 1 running + 0 starting (1.5 s later)
  create 3: HTTP 503 'no node has capacity'
  => node-a holds 1, node-b holds 1 (limit 1 per node)
[02:37:39] deleted 2 sandbox(es) directly on the nodes
[02:37:44] scheduler says both nodes are empty (5.0 s later)

== Step 3: a create the node rejects, then 2 creates at once
  create with body {}: HTTP 422 'Failed to deserialize the JSON body into'
  create 1: HTTP 201, sandbox 01a17f30-cd76-78d3-851b-f832d09763f3 on node-a
  create 2: HTTP 201, sandbox 01a17f30-cd73-7271-90aa-708d3bdc311e on node-b
  => node-a holds 1, node-b holds 1 (limit 1 per node)

[02:37:52] cleanup: deleted 2 sandbox(es)
[02:37:52] restarted the scheduler with services/config/lima.json alone: pid 86103, log /tmp/verify-bindings-scheduler.log

== Result
PASS: no node got more than 1 sandbox, and the rejected create kept no slot.
```

A run at 02:34 gave the same result.

## Throughput of one scheduler (2026-10-29)

[scripts/leader_throughput_bench_test.go](scripts/leader_throughput_bench_test.go)
calls `Schedule` in process (no network) from 15 goroutines on an Apple M5
Pro. Every node is ready and never full. "place" also keeps 5 reservations
per node waiting for a heartbeat, about 5 s of creates; "no-limits" runs
without `node_resource_limit`, so without the lock.

| Nodes | place | place, nothing pending | no-limits |
| --- | --- | --- | --- |
| 100 | 17,390/s | 29,576/s | 45,417/s |
| 1,000 | 2,365/s | 3,874/s | 4,822/s |
| 5,000 | 476/s | 887/s | 1,063/s |

With the Redis binding write as well (1,000 nodes, its own `redis-server`):
2,166/s.

```text
cpu: Apple M5 Pro
BenchmarkLeaderCreates/place/nodes=100-15 17390 creates/s
BenchmarkLeaderCreates/place-no-inflight/nodes=100-15 29576 creates/s
BenchmarkLeaderCreates/no-limits/nodes=100-15 45417 creates/s
BenchmarkLeaderCreates/place/nodes=1000-15 2365 creates/s
BenchmarkLeaderCreates/place-no-inflight/nodes=1000-15 3874 creates/s
BenchmarkLeaderCreates/no-limits/nodes=1000-15 4822 creates/s
BenchmarkLeaderCreates/place/nodes=5000-15 476.3 creates/s
BenchmarkLeaderCreates/place-no-inflight/nodes=5000-15 886.7 creates/s
BenchmarkLeaderCreates/no-limits/nodes=5000-15 1063 creates/s
BenchmarkLeaderCreates/place+redis/nodes=1000-15 2166 creates/s
ok      agentenv/services/scheduler/internal    27.560s
```

Each create looks at every node and, with limits on, adds up every pending
reservation under the lock, so the rate falls as the cluster grows. Two
earlier runs gave the same numbers within a few percent.
