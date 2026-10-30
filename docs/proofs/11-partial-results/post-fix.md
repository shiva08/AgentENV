# 11. Partial results: after the fix

Proofs for section 11 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps.

## Unit tests

Run against the code before the fix, the new test fails; the other two pin
behavior that stays the same, so they pass before and after:

```text
=== RUN   TestHandleProxyClusterListSkipsNodesThatDoNotAnswer
    server_test.go:1382: /sandboxes: status = 502, want 200
--- FAIL: TestHandleProxyClusterListSkipsNodesThatDoNotAnswer (0.00s)
--- PASS: TestHandleProxyClusterListFailsWhenNoNodeAnswers (0.00s)
--- PASS: TestHandleProxyClusterListReturnsANodesClientError (0.00s)
--- PASS: TestHandleProxyClusterListPropagatesUnauthorized (0.00s)
```

With the fix, all cluster-list tests pass, also 3 times in a row with the race
detector (`go test -race -count=3`). `make -C services fmt-check vet` and
`go test ./...` in `services/` pass:

```text
--- PASS: TestHandleProxyAggregatesSandboxListAcrossNodes (0.00s)
--- PASS: TestHandleProxyClusterListPreservesNodeFields (0.00s)
--- PASS: TestHandleProxyAggregatesV2SandboxesWithGlobalPagination (0.00s)
--- PASS: TestHandleProxyAggregatesV2SandboxesAscendingPagination (0.00s)
--- PASS: TestHandleProxyAggregatesSandboxListDedupsDuplicateSandboxIDs (0.00s)
--- PASS: TestHandleProxyClusterListSkipsNodesThatDoNotAnswer (0.00s)
--- PASS: TestHandleProxyClusterListFailsWhenNoNodeAnswers (0.00s)
--- PASS: TestHandleProxyClusterListReturnsANodesClientError (0.00s)
--- PASS: TestHandleProxyClusterListPropagatesUnauthorized (0.00s)
ok      agentenv/services/gateway/internal      0.872s
```

## Output after the fix (2026-10-29 00:42 IST)

The fixed gateway, started as in [usage.md](../../usage.md). Exit code 0:

```text
run 55c255ec829e: gateway http://127.0.0.1:8090; node-a = VM agentenv http://127.0.0.1:8000, node-b = VM agentenv-2 http://127.0.0.1:8001
Each sandbox: POST /sandboxes-cold, ubuntu:22.04, 1 vCPU, 512 MiB, label repro-run=55c255ec829e.
[00:42:10] both nodes ready (0.0 s)

== Setup: one sandbox on each node
  create 1: HTTP 201, sandbox 01a183ed-5c78-7a10-aae5-fe3a2e7ee2ca on node-b
  create 2: HTTP 201, sandbox 01a183ed-7bd5-7f60-a2f7-c36282098997 on node-a

== Step 1: both nodes up
  GET /v2/sandboxes: HTTP 200 in 0.00 s; sandboxes: ...82098997 on node-a, ...2e7ee2ca on node-b
  GET /sandboxes: HTTP 200 in 0.00 s; sandboxes: ...82098997 on node-a, ...2e7ee2ca on node-b

== Step 2: node-b's server stopped
[00:42:26] systemctl stop aenv on node-b: done
  GET /v2/sandboxes: HTTP 200 in 0.01 s; sandboxes: ...82098997 on node-a; x-agentenv-unavailable-nodes: node-b
  GET /sandboxes: HTTP 200 in 0.00 s; sandboxes: ...82098997 on node-a; x-agentenv-unavailable-nodes: node-b

== Step 3: node-b started again
[00:42:26] node-b ready again (0.5 s after the start)
  GET /v2/sandboxes: HTTP 200 in 0.01 s; sandboxes: ...82098997 on node-a, ...2e7ee2ca on node-b
  GET /sandboxes: HTTP 200 in 0.00 s; sandboxes: ...82098997 on node-a

[00:42:27] cleanup: deleted 2 sandbox(es) directly on the nodes

PASS: with node-b down, both lists returned node-a's sandbox and named node-b in x-agentenv-unavailable-nodes; with it back, the full list returned.
```

The gateway logged each node it left out:

```text
2026-10-29T00:42:26.757+0530    warn    internal/cluster_list.go:205    cluster sandbox list left out a node    {"node_id": "node-b", "error": "perform upstream request: Get \"http://127.0.0.1:8001/v2/sandboxes?metadata=repro-run%3D55c255ec829e&state=running&state=paused\": dial tcp 127.0.0.1:8001: connect: connection refused"}
2026-10-29T00:42:26.763+0530    warn    internal/cluster_list.go:205    cluster sandbox list left out a node    {"node_id": "node-b", "error": "perform upstream request: Get \"http://127.0.0.1:8001/sandboxes?metadata=repro-run%3D55c255ec829e\": dial tcp 127.0.0.1:8001: connect: connection refused"}
```
