# 6. Retry: current behavior (before the fix)

Proofs for section 6 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md).

## How the run was made

The script from point 5 has a mode for this case:

```bash
python3 docs/proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py --mode crash-now
```

Step 2 kills node-b's server, waits until its port refuses connections, checks
that the scheduler still lists node-b as `ready`, and sends 4 creates at once.
Exit status 1 means a create went to node-b; 0 means every create reached a
running node. Step 3 also waits until node-b answers `/health`, because in
this mode node-b never stops looking `ready`.

The wait matters because of Lima. Lima forwards node-b's port to the Mac, and
for a moment after a crash the forward still accepts connections and then
drops them. Probing node-b's port every 0.25 s after a kill (curl exit `52` =
connection accepted, then an empty reply; `7` = connection refused):

```text
+ 0.12s curl exit 52  000
+ 0.45s curl exit 52  000
+ 0.75s curl exit 52  000
+ 1.04s curl exit 52  000
+ 1.33s curl exit 52  000
+ 1.62s curl exit 52  000
+ 1.91s curl exit 52  000
+ 2.21s curl exit 52  000
+ 2.51s curl exit 7  000
+ 2.81s curl exit 7  000
+ 3.10s curl exit 7  000
+ 3.39s curl exit 7  000
+ 3.69s curl exit 7  000
+ 3.98s curl exit 7  000
+ 4.27s curl exit 7  000
+ 4.56s curl exit 7  000
+ 4.86s curl exit 7  000
+ 5.16s curl exit 7  000
+ 5.45s curl exit 7  000
+ 5.74s curl exit 7  000
+ 6.03s curl exit 7  000
+ 6.33s curl exit 7  000
+ 6.61s curl exit 7  000
+ 6.91s curl exit 7  000
node-b restored: 204
```

On a real machine the kernel refuses connections to a dead process at once.

## Output before the fix (2026-10-27 02:49 IST)

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[02:49:54] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[02:49:54] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode crash-now)
[02:49:54] killed node-b's server (SIGKILL): it no longer serves or sends heartbeats
[02:49:54] waiting until node-b's port refuses connections ...
[02:49:54] node-b's port refuses connections, but scheduler still says: node-a=ready node-b=ready   (0 s after the kill)
  create 1: HTTP 502 'upstream unavailable'               -> reached no running node, so it went to node-b (down)
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 502 'upstream unavailable'               -> reached no running node, so it went to node-b (down)
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[02:49:54] scheduler still says: node-a=ready node-b=ready
  => node-a got 2, node-b got 2
     The 2 create(s) sent to node-b failed with HTTP 502 and created nothing.
     The gateway did not try node-a instead: node-a's counter did not move for them.

== Step 3: node-b healthy again
[02:49:54] started node-b's server
[02:49:55] scheduler says: node-a=ready node-b=ready   (1 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Result
BUG REPRODUCED: 2 of 4 creates went to node-b after it crashed, while the scheduler still listed it as ready.
```

## Right after a scheduler restart (2026-10-27 03:06 IST)

A background loop sent a create with body `{}` through the gateway every
0.2 s and printed only changes in the reply, while the scheduler was stopped
and started again at once.

Scheduler log:

```text
2026-10-27T03:06:03.517+0530    info    cmd/main.go:130 scheduler shutdown signal received
2026-10-27T03:06:03.518+0530    info    cmd/main.go:145 scheduler stopped gracefully
2026-10-27T03:06:03.866+0530    info    cmd/main.go:93  scheduler gRPC server listening {"addr": "127.0.0.1:9090", "strategy": "round_robin", "binding_store": "memory", "query_only": false}
```

What the creates got (first 60 characters of each reply):

```text
03:05:57.111 HTTP 422 Failed to deserialize the JSON body into the target type: mi
03:06:03.714 HTTP 503 connection error: desc = "transport: Error while dialing: di
03:06:04.738 HTTP 503 no ready nodes
03:06:07.202 HTTP 422 Failed to deserialize the JSON body into the target type: mi
```

| Time | Creates got | Why |
| --- | --- | --- |
| Up to 03:06:03.5 | `422` from a node | Normal |
| 03:06:03.7 to 03:06:04.7 | `503` connection error | The scheduler was down for 0.35 s; the gateway's connection to it took about 1 s more to come back |
| 03:06:04.7 to 03:06:07.2 | `503 no ready nodes` | The scheduler was up but had no heartbeats yet |
| From 03:06:07.2 | `422` from a node | The first heartbeat arrived |

Creates were refused for about 3.5 s, and none was sent to a node.
