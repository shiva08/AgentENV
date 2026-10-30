# 5. Failure detection: current behavior (before the fix)

Proofs for section 5 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md).

## How the runs were made

[scripts/reproduce-unhealthy-node-scheduling.py](scripts/reproduce-unhealthy-node-scheduling.py)
needs only the Python standard library and `limactl`.

```bash
python3 docs/proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py                  # --mode crash, the default
python3 docs/proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py --mode silence
```

- **Step 1:** both nodes are `ready`; send 4 creates.
- **Step 2:** node-b stops sending heartbeats. Wait until the scheduler says
  `unhealthy`, then send 4 creates.
  - `crash`: SIGKILL node-b's server and keep it stopped, so it can't
    unregister.
  - `silence`: a temporary nftables table in node-b's VM rejects its traffic
    to the scheduler (port 9090). node-b keeps serving.
- **Step 3:** undo step 2, wait until node-b is `ready`, send 4 creates.

Each create is `POST /sandboxes-cold` with body `{}`. A node that receives it
answers `422`, so nothing is created. To show which node got each create, the
script reads each node's own request counter
(`agentenv_http_request_duration_seconds_count` for `POST /sandboxes-cold` in
`GET /metrics`) before and after it. A create that returned `502` and that no
running node counted went to the stopped node.

Exit status: 1 = bug reproduced, 0 = no create went to node-b while it was
`unhealthy`, 2 = setup error. The API key comes from `AENV_API_KEY`, or from
node-a's VM when that is unset. The script restores node-b on exit (including
Ctrl-C) and before it starts, so an interrupted run doesn't break the next one.

## Five runs (2026-10-27 IST)

| Run | Mode | node-b `unhealthy` after | Step 2: creates sent to node-b | Exit |
| --- | --- | --- | --- | --- |
| 00:03:44 | crash | 29 s | 2 of 4, both `502` | 1 |
| 00:04:22 | silence | 28 s | 2 of 4, both answered by node-b (`422`) | 1 |
| 00:05:08 | crash | 27 s | 2 of 4, both `502` | 1 |
| 00:05:55 | silence | 26 s | 2 of 4, both answered by node-b (`422`) | 1 |
| 00:10:56 | crash, no arguments and no `AENV_API_KEY` | 30 s | 2 of 4, both `502` | 1 |

In every run, steps 1 and 3 split the creates 2 and 2.

Crash run with no arguments, 00:10:56 (exit code 1):

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[00:10:56] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[00:10:56] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode crash)
[00:10:56] killed node-b's server (SIGKILL): it no longer serves or sends heartbeats
[00:10:57] waiting until the scheduler marks node-b unhealthy (no heartbeat for 30 s) ...
[00:11:27] scheduler says: node-a=ready node-b=unhealthy   (30 s after node-b went silent)
  create 1: HTTP 502 'upstream unavailable'               -> reached no running node, so it went to node-b (down)
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 502 'upstream unavailable'               -> reached no running node, so it went to node-b (down)
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[00:11:27] scheduler still says: node-a=ready node-b=unhealthy
  => node-a got 2, node-b got 2
     The 2 create(s) sent to node-b failed with HTTP 502 and created nothing.
     The gateway did not try node-a instead: node-a's counter did not move for them.

== Step 3: node-b healthy again
[00:11:27] started node-b's server
[00:11:28] scheduler says: node-a=ready node-b=ready   (1 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Result
BUG REPRODUCED: the scheduler sent 2 of 4 creates to node-b while it marked node-b unhealthy.
```

Silence run, 00:05:55 (exit code 1). node-b's own counter shows that it
received creates 1 and 3 while the scheduler showed it as `unhealthy`:

```text
gateway http://127.0.0.1:8090; node-a = VM agentenv, http://127.0.0.1:8000; node-b = VM agentenv-2, http://127.0.0.1:8001
Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,
so nothing is created. Each node's request counter (GET /metrics) shows who got it.

[00:05:55] preparing: make sure node-b is running and its heartbeats are not blocked

== Step 1: both nodes healthy
[00:05:55] scheduler says: node-a=ready node-b=ready
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Step 2: node-b stops sending heartbeats (--mode silence)
[00:05:55] blocked node-b's traffic to the scheduler (port 9090): it keeps serving, but its heartbeats fail
[00:05:55] waiting until the scheduler marks node-b unhealthy (no heartbeat for 30 s) ...
[00:06:21] scheduler says: node-a=ready node-b=unhealthy   (26 s after node-b went silent)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
[00:06:21] scheduler still says: node-a=ready node-b=unhealthy
  => node-a got 2, node-b got 2
     node-b is still running, so it answered the 2 create(s) sent to it.

== Step 3: node-b healthy again
[00:06:21] removed the block; node-b retries heartbeats with backoff (up to 60 s)
[00:06:31] scheduler says: node-a=ready node-b=ready   (10 s later)
  create 1: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 2: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  create 3: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-b
  create 4: HTTP 422 'Failed to deserialize the JSON b'   -> reached node-a
  => node-a got 2, node-b got 2

== Result
BUG REPRODUCED: the scheduler sent 2 of 4 creates to node-b while it marked node-b unhealthy.
```

## First check, by hand (2026-10-26 23:44 IST)

Before the script existed, each case sent 6 creates with `curl` and body `{}`.
This also covered a clean stop, which the script does not: node-b
unregistered, disappeared from `GET /nodes` at once, and still got every other
create.

Clean stop (`systemctl stop aenv` on node-b, which unregisters it):

```text
23:44:09 before: node-a=ready node-b=ready
23:44:10 node-b AgentENV service stopped
23:44:10 scheduler now says: node-a=ready
create 1: HTTP 502  upstream unavailable
create 2: HTTP 422  Failed to deserialize the JSON body into
create 3: HTTP 502  upstream unavailable
create 4: HTTP 422  Failed to deserialize the JSON body into
create 5: HTTP 502  upstream unavailable
create 6: HTTP 422  Failed to deserialize the JSON body into
```

Crash (`systemctl kill -s SIGKILL aenv`, then kept down, so no unregister):

```text
23:44:38 before: node-a=ready node-b=ready
23:44:38 node-b server killed (no clean shutdown, so no unregister)
23:45:07 scheduler now says: node-a=ready node-b=unhealthy
create 1: HTTP 502  upstream unavailable
create 2: HTTP 422  Failed to deserialize the JSON body into
create 3: HTTP 502  upstream unavailable
create 4: HTTP 422  Failed to deserialize the JSON body into
create 5: HTTP 502  upstream unavailable
create 6: HTTP 422  Failed to deserialize the JSON body into
```

After `systemctl start aenv`, node-b was `ready` again within 2 s.
