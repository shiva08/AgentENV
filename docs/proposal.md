# AgentENV: toward production readiness

**AgentENV** is an open-source platform that gives AI agents sandboxes:
isolated Linux microVMs that start in seconds, run the agent's code, and can
be paused, snapshotted, resumed and forked. It has an E2B-compatible API, and
a gateway and a scheduler spread the sandboxes over many machines.

## Architecture

### Current

```text
client ──▶ gateway ── "which node?" ──▶ scheduler
              │                            ▲
              │ forwards the request        │ heartbeat every 5 s from each node:
              ▼                            │ health, free capacity, list of sandbox IDs
         node-a, node-b, ... ────────────────┘
         (each node runs sandboxes as microVMs)
```

- **New sandbox:** the gateway asks the scheduler for a node. The scheduler
  picks one (round-robin by default), the gateway forwards the create there,
  and then tells the scheduler which node holds the sandbox.
- **Existing sandbox:** the gateway asks the scheduler which node holds it,
  and forwards the request there.
- The scheduler keeps this sandbox-to-node table in memory. Each heartbeat's
  list of sandbox IDs keeps it up to date.
- **Restore:** a restarted server reloads its paused sandboxes from its own
  disk. Nothing restores a sandbox on another node; the scheduler places only
  new sandboxes. An agent can launch one of its snapshots on another node
  itself, under a new ID, and only if snapshots are in S3.

### Proposal

The numbers in brackets are the points below; 5 to 11 are built, 1 to 4 are
designs. Point 12, a plan for an internal managed service, is sketched in
section 12.

```text
client ──▶ gateways ──── "which node?" ────▶ scheduler: leader + standbys (3)
             │  retry on another node (6)        │  only ready nodes (5)
             │  lists skip a down node (11)      │  reserve room in one step (7)
             │  Idempotency-Key (8)              │  restore a dead node's sandboxes (2)
             │                                   │
             │       Redis + replica (4): bindings, Idempotency-Key replies
             │                                   ▲
             ▼                                   │ heartbeats; lists written only on change (10)
           nodes ────────────────────────────────┘
             │  snapshots to disk often; exported to S3 less often (1)
             │  a cancelled restore cleans up after itself (9)
             ▼
           S3: snapshots; any node can restore any sandbox (1, 2)
```

## Summary

**What breaks today**

- A server crash loses every running sandbox.
- A dead machine's sandboxes are lost or stuck until it returns.
- If the scheduler or Redis goes down, agents cannot reach sandboxes that are
  still running.

**Proposal**

- Snapshot running sandboxes automatically, to the node's disk and to S3.
- Relaunch a dead machine's sandboxes on other nodes.
- Scheduler HA (active-passive) and Redis HA: standby schedulers and a Redis
  replica that take over by themselves.
- Later, offer AgentENV as an internal managed service for many teams.

**Already built (7 fixes)**

- New sandboxes skip dead nodes, and full ones when a per-node limit is set.
- Retries with an `Idempotency-Key` no longer make duplicates.
- One failing node, late heartbeat or cancelled restore no longer breaks
  lists, lookups or later restores.

**Evidence**

- 11 gaps were found on a two-node cluster by fault injection (killing
  processes and machines, stopping Redis, dropping replies, cancelling
  restores) or by reading the code. The list is not exhaustive.
- Seven are fixed, unit-tested and re-run after the fix; four are designed,
  not built. Point 12 is a plan, not a gap.
- All runs used two Lima VMs on one Mac, and the Kubernetes parts are
  unit-tested only.

Proofs and scripts: [proofs/](proofs/). Test cluster: [usage.md](usage.md).

| # | Point | Status | Problem | Solution |
| --- | --- | --- | --- | --- |
| 1 | [Automatic snapshots](#1-backup-durability-a-server-crash-loses-every-running-sandbox) | Approach suggested | • Snapshots are taken only when the agent asks for one or the sandbox pauses, and stored on the node's disk.<br>• Server crash: paused sandboxes can be recovered, running ones can't.<br>• Machine crash: nothing can be recovered elsewhere; paused sandboxes come back only with the machine. | • Snapshot every running sandbox to the node's disk, often; a pause is saved there already.<br>• Export disk snapshots to S3 less often: a background exporter uploads each sandbox's newest changed snapshot every few minutes.<br>• Server crash: the restarted server restores from its disk. |
| 2 | [Restore after a machine dies](#2-restore-recoverability-when-a-nodes-machine-dies-nothing-relaunches-its-sandboxes-or-tells-the-agent) | Approach suggested | When a node's machine dies (power or hardware failure):<br>• Nothing relaunches its sandboxes; running ones are lost.<br>• Every request for them gets `502`. | • Nodes send a heartbeat every 5 s; after about 5 minutes with none, the scheduler treats the node as dead.<br>• Fencing: it powers the node off, and each sandbox gets a generation number that goes up at each restore, so an old copy is refused when its node comes back.<br>• The scheduler relaunches each sandbox from its newest S3 snapshot on another node, under the same ID.<br>• If more than half the nodes go silent at once, it fences nobody: it is more likely the one cut off. |
| 3 | [Scheduler high availability](#3-high-availability-scheduler-if-the-scheduler-dies-the-cluster-is-headless) | Approach suggested | • Every request asks the one scheduler where to go.<br>• If it is down, every request fails with `503` until it is back. | • Sandbox-to-node bindings placed in Redis.<br>• **Active-passive:** 3 schedulers with Kubernetes leader election; a standby takes over in about 15 s.<br>• Changes of state (create, pause, resume, snapshot, delete) go to the leader.<br>• Sandbox lookups go to any replica; they need Redis (section 4).<br>• Fencing: every write carries the leader's term number, so an old leader's writes are refused.<br>• Gateways keep no state, so several run behind a load balancer.<br>• At scale: many cells, each with its own leader, all active at once. |
| 4 | [Redis high availability](#4-high-availability-redis-if-redis-is-down-agents-cannot-reach-their-sandboxes) | Approach suggested | • One Redis holds the bindings and the `Idempotency-Key` replies.<br>• If it is down, every call on an existing sandbox fails with `503` until it is back.<br>• It also made nodes look dead after about a minute, so creates failed too; that part is fixed. | • Redis with a replica that takes over by itself.<br>• `Idempotency-Key` replies in their own Redis, saved to disk, so a failover is less likely to lose them.<br>• Built already: heartbeats are accepted when the Redis write fails, so nodes stay `ready`. |
| 5 | [Failure detection](#5-fault-tolerance-failure-detection-a-dead-node-keeps-getting-new-sandbox-requests) | Implemented | A crashed or stopped node still got every other new sandbox, and those creates failed with `502`. | • New sandboxes go only to nodes whose last heartbeat is under 30 s old.<br>• If there is none, `503` at once. |
| 6 | [Retry](#6-retry-fault-tolerance-creates-sent-to-a-dead-node-fail-although-another-node-is-up) | Implemented | • In the first 30 s after a node crash, before it is marked down, creates sent to it failed.<br>• After a scheduler restart, every create failed for a few seconds. | • If the gateway cannot connect to a node (refused, or no answer within 2 s, as when its machine is off), nothing was created, so it sends the create to another node, once.<br>• For its first 30 s, the scheduler also uses nodes it has not heard from yet. |
| 7 | [Capacity check](#7-atomic-capacity-check-admission-control-parallel-creates-overfill-a-node) | Implemented | Creates arriving together all saw the same free space, so a node could get more sandbox requests than its limit allows. | • The scheduler checks free capacity and reserves it in one locked step.<br>• Sandboxes still starting count.<br>• When every node is full: `503 no node has capacity`.<br>• Only when `node_resource_limit` is set; no shipped config sets it. |
| 8 | [Idempotency](#8-idempotency-safe-retries-retrying-a-create-can-make-a-duplicate-sandbox) | Implemented | If a create's reply is lost, the agent retries, and the retry makes a second sandbox. | • The client sends an `Idempotency-Key`.<br>• The gateway keeps the first reply in Redis, with a hash of the request.<br>• A retry with the same key and the same request gets that reply; the same key with a different request gets `422`. |
| 9 | [Cancellation safety](#9-resilience-cancellation-safety-one-cancelled-restore-breaks-a-snapshot-on-its-node-until-the-server-restarts) | Implemented | • If a client hung up during a restore, its snapshot download stayed "in progress" forever.<br>• Every later restore of that snapshot on that node failed until the server restarted. | • The next restore removes the abandoned download and starts it again.<br>• Downloads go to a temporary file, renamed when complete, so a half-written file is never used. |
| 10 | [Reconciliation](#10-reconciliation-efficiency-and-correctness-every-heartbeat-rewrote-all-bindings-in-redis-which-expired-after-30-s) | Implemented | • Every heartbeat (every 5 s) rewrote every sandbox's binding (sandbox → node) in Redis.<br>• Bindings expired after 30 s, so a late heartbeat made running sandboxes answer `404`. | • The scheduler writes a node's list to Redis only when its hash changes.<br>• Bindings now last a year.<br>• If Redis loses its data, every list is written again within about 5 s. |
| 11 | [Partial results](#11-graceful-degradation-partial-results-one-node-down-makes-the-list-of-all-sandboxes-fail) | Implemented | • Listing all sandboxes asks every node.<br>• With one node down, the whole list failed with `502` for everyone. | • Return the sandboxes from the nodes that answered.<br>• Name the missing node in the `x-agentenv-unavailable-nodes` header. |
| 12 | [Internal managed service](#12-internal-managed-service) | Approach suggested | • AgentENV is run by one operator for one group.<br>• Everyone shares one API key: no owners or limits. | • **Control plane:** each region has gateways, a scheduler leader with standbys, and Redis.<br>• **Data plane:** each region's KVM nodes and S3 bucket.<br>• Every sandbox has an owner team; limits per team.<br>• Lists read each team's list in Redis instead of asking every node. |

## Details

### 1. Backup (durability): a server crash loses every running sandbox

**Status:** approach suggested, not built.

#### Problem

- Snapshots are taken only when the agent asks for one or the sandbox pauses,
  and by default they stay on the node's own disk.
- A running sandbox's record is only in the server's memory, so when the
  server crashes, the agent loses all the work in it: its files, processes
  and memory.

| | The server dies, the machine is alive | The machine dies |
| --- | --- | --- |
| Example | A bug or out-of-memory kills the server; systemd starts it again | Power or hardware failure |
| Running sandboxes | Lost: the agent gets `404` | Lost (section 2) |
| Paused sandboxes | Kept on the node's disk | Gone until the machine comes back |

A planned restart loses nothing: the server pauses every sandbox first.

#### Solution

A background exporter on each node snapshots every running sandbox to its
disk on a timer (a pause is saved there already) and, every few minutes,
exports the newest changed snapshots to S3, a few at a time. Steps, in order:

1. Measure the pause a snapshot causes and its disk writes, then choose the
   timer: both land on the agent's running work.
2. Keep both snapshot stores, disk and S3 (a node has one today), and add the
   exporter.
3. Save running sandboxes' records to disk too; today only paused ones are.
4. At startup: clean up the old server's leftover processes, restore the
   node's sandboxes from its disk, and list them in heartbeats from the start.
5. Delete old snapshots and the layers no snapshot uses; today deleting a
   snapshot keeps its layers, so storage grows without limit.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Disk often, export to S3 less often (chosen) | Fast restore from disk after a server crash; S3 survives the machine |
| S3 or a shared filesystem only | Works, only slower: every snapshot crosses the network (a 512 MiB sandbox launched from S3 in 2 s, in section 2's proofs) |

#### Behavior after the fix

Expected: every sandbox always has a snapshot on its node's disk, at most one
disk interval old, and a copy in S3 about one export interval old.

**Proofs:** [before the fix](proofs/01-backup/current.md), by hand.

### 2. Restore (recoverability): when a node's machine dies, nothing relaunches its sandboxes or tells the agent

**Status:** approach suggested, not built; it builds on section 1. Problem
reproduced.

#### Problem

Nothing restores a dead machine's sandboxes on a new node:

- Every agent with a sandbox on that machine stops: running work is lost,
  and paused work is stuck until the machine comes back.
- The agent only gets `502`, the same as for a brief network glitch, so it
  cannot tell the sandbox is gone.

#### Solution

Six mechanisms, all needed:

| # | Mechanism | What it does |
| --- | --- | --- |
| 1 | **Backup** | Disk snapshots are exported to S3 every few minutes (section 1) |
| 2 | **Failure detection** | About 5 minutes without heartbeats means the machine is dead, not slow. Today's 30 s `unhealthy` only keeps new sandboxes away (section 5) |
| 3 | **Fencing** | Stops the old copy from ever acting again:<br>• The dead machine is powered off from outside.<br>• Each sandbox gets a generation number that goes up at each restore. The number is kept with the sandbox's binding in Redis and in its record on the node; the scheduler refuses a copy with a lower number, and if Redis loses its data, the highest number a node reports wins.<br>• A starting node asks the scheduler which sandboxes are still its own and restores only those.<br>• Today a node that comes back reloads its paused sandboxes and points their records back at itself ([architecture.md](../docs/src/internals/architecture.md#L214)). |
| 4 | **Restore orchestrator** | A background loop in the scheduler. Nodes report each sandbox's newest snapshot in S3 in their heartbeats; the loop launches it on a node with room. A paused sandbox is launched at its next resume; one without a snapshot is marked lost |
| 5 | **Rebinding** | The snapshot is launched under the old ID (today every launch gets a new ID), and the record points to the new node. The access-token seed shared by all nodes keeps the sandbox's tokens valid |
| 6 | **Telling the agent** | After 30 s, `503` "node unreachable, restore pending" instead of `502`; with no snapshot, `410 Gone` |

The whole flow, once built:

```text
node-b's machine dies holding A (running; newest snapshot S in S3, 10 min old)
and B (paused; its pause already exported to S3)

+0 s     requests for A and B: 502, as today; nobody knows yet
+30 s    scheduler: node-b unhealthy, no new sandboxes go there (exists, section 5)
         requests for A and B: 503 "node unreachable, restore pending"
+5 min   scheduler: node-b is dead
         fencing: power node-b off through the cloud API, wait until it is off
         A: launch S on node-a under the ID A, generation 2; A's record → node-a
         B: stays paused, in S3; its next resume launches it on a node with room
+5 min   agent: GET A → 200 running, restored from S; its last 10 minutes of work are lost
later    node-b comes back and, before restoring anything, asks the scheduler which
         sandboxes are still its own → none: A and B moved (generation 2)
         → node-b deletes its copies of A and B without starting them
```

##### Approaches considered

| Question | Chosen, and why |
| --- | --- |
| How to fence | Power off from outside (STONITH, "shoot the other node in the head") plus generation numbers. A lease would stop every node's sandboxes during a scheduler outage; generation numbers alone do not stop a node that is cut off but still running |
| Who restores | The scheduler, not a separate monitor: it already knows node health and each node's sandboxes. A monitor fits one job only: powering off the dead machine |
| Same ID or new ID | Same ID: agents and SDKs that hold it keep working |
| Wait before restoring | About 5 minutes, Kubernetes' default before it moves pods off an unreachable machine. Shorter risks two copies of a slow node |
| Many nodes silent at once | • If more than half go silent together, fence and restore nothing: the scheduler is more likely the one cut off. Otherwise one node at a time.<br>• Kubernetes does the same: it stops evicting when every node looks down, slows down when 55% of a zone does, and evicts at most one node per 10 s by default ([nodes](https://kubernetes.io/docs/concepts/architecture/nodes/#rate-limits-on-eviction)).<br>• A new leader counts silence from when it took over, since it has no older heartbeats.<br>• When nothing is restored, an operator confirms the machines are off and marks them dead with an admin call (to build); only then are their sandboxes restored. Kubernetes asks the same of operators with its `out-of-service` taint ([node shutdown](https://kubernetes.io/docs/concepts/cluster-administration/node-shutdown/#non-graceful-node-shutdown-handling)). |
| Restore at node startup | Ask the scheduler first. Restoring everything and deleting later would run second copies until the first heartbeat; the cost is that a node starting while the scheduler is down restores nothing |

#### Behavior after the fix

Expected, as in the flow above: a dead machine's running sandboxes run again
on another node about 5 minutes after it dies (recovery time), and lose the
work since their last export to S3, at most about one export interval
(recovery point).

**Proofs:** [before the fix](proofs/02-restore/current.md),
[script](proofs/02-restore/scripts/reproduce-lost-node-sandboxes.py).

### 3. High availability (scheduler): if the scheduler dies, the cluster is headless

**Status:** approach suggested, not built. Problem checked by hand.

#### Problem

- There is one scheduler, and the gateway asks it about every request: which
  node gets a new sandbox, and which node holds an existing one.
- If it dies, the cluster is "headless": every request through the gateway
  fails with `503` (creates, every call on an existing sandbox, lists,
  `GET /nodes`) until it is back, although the sandboxes keep running:

```text
scheduler up:    agent → gateway → scheduler: "sbx-1 is on node-a" → node-a   works
scheduler down:  agent → gateway → scheduler (dead) → 503                     sbx-1 still running on node-a
```

- A crashed process is back within seconds (3.3 s by hand); a dead machine
  takes until it returns, or about 5 minutes on Kubernetes.
- Redis is a second single point of failure (section 4).

#### Solution

- Run 3 schedulers, active-passive, with leader election: only the one
  holding a Kubernetes `Lease` places sandboxes.
- If it dies, another takes over, after about 15 s by default.

**Active-passive or active-active:**

| | Pros | Cons |
| --- | --- | --- |
| **Active-passive (chosen):** one leader places sandboxes; the others stand by | • Only leader election to build.<br>• Node limits stay exact: one lock, in one process.<br>• Placing a sandbox needs no Redis. | • When the leader dies, changes of state wait about 15 s; calls inside sandboxes keep working.<br>• One process places every create: today about 2,400 per second with 1,000 nodes and 480 with 5,000 (section 7). |
| **Active-active:** every scheduler places sandboxes | • No pause in creates when a scheduler dies.<br>• No leader election; any scheduler takes any request. | • Much more to build: everything the leader learns from heartbeats moves to Redis, and every create runs a Lua script there, a script Redis runs as one atomic step (section 7).<br>• Not faster: Redis runs one script at a time, so creates still queue in one place.<br>• Creates fail whenever Redis is down. |

Chosen: active-passive inside each cell, and at scale many cells (separate
groups of machines, each with its own scheduler leader and Redis), all active
at once. While one cell changes leader, the gateway can send new creates to
the other cells (to build). Active-active inside a cell only if its leader
cannot keep up, even after the fix in section 7.

**The leader handles every change of state,** so two changes to one sandbox
never conflict, such as a resume that arrives while the leader is restoring
that sandbox on another node (section 2).

| Request | Handled by |
| --- | --- |
| Changes of state: create, fork, pause, resume, snapshot, delete | The leader only |
| Node heartbeats | The leader only |
| Calls inside a sandbox (commands, files) | Any scheduler: it only answers "which node has this sandbox?" from Redis, and the call then goes straight to that node. So these calls need Redis (section 4) |
| Sandbox lists | Any scheduler (to build): a list needs only the node addresses, from discovery |
| `GET /nodes` | The leader: it shows heartbeat data, which only the leader receives |

Only the leader's readiness check passes, so the scheduler's main address
(`gateway.scheduler_addr`) reaches only the leader; lookups use
`gateway.query_only_scheduler_addr`, which already exists.

**A scheduler that loses the lease stops at once.** Otherwise an old leader
cut off from Kubernetes keeps getting creates over open gRPC connections, and
two schedulers would place sandboxes.

**Fencing:** stopping is not enough: `client-go`'s leader election does not
guarantee a single leader
([leaderelection](https://pkg.go.dev/k8s.io/client-go/tools/leaderelection)),
since a leader frozen by a long pause can wake up after its lease ran out.
So every write carries the leader's term number (the `Lease`'s transition
count), and Redis and the nodes refuse lower numbers.

**State:**

- Bindings (which node has each sandbox): Redis, since every scheduler reads
  them.
- Capacity reservations (section 7): the leader's memory, since only the
  leader places sandboxes.

**Gateways:**

- They keep no state, so 2 or more run behind a load balancer; the shipped
  manifest runs 1.
- One that dies fails only its open requests (section 8).

**Steps, in order:**

1. No code: run query-only replicas with Redis and set
   `gateway.query_only_scheduler_addr`, so existing sandboxes stay usable
   during an outage; creates, lists and `GET /nodes` wait.
2. Redis HA (section 4), since every scheduler replica reads Redis.
3. On Kubernetes: leader election built into the scheduler (`client-go`'s
   `leaderelection`, with permission to read and update `Lease` objects),
   with 3 replicas, and the leader's term number on every Redis write
   (fencing). With it:
   - the startup grace (section 6) must start when a scheduler becomes
     leader, not at process start: a standby running for hours keeps no node
     state, so it would answer `503` until the nodes' heartbeats reach it, by
     then 10 to 20 s apart;
   - a new leader has no reservations, so the over-capacity window of a
     scheduler restart (section 7) reopens at every leader change.
4. With leader election: nodes send their CPU features again to each new
   leader, which combines them so a snapshot from one node runs on any other.
   Today a node sends them only once, after it starts.
5. Pauses, resumes, forks, snapshots and deletes through the leader, as
   creates already are.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| The agent calls the node directly | No: the agent does not know the node (the create reply's `clientID` is always empty), and nodes are usually not reachable from outside |
| The agent reads the sandbox's node from Redis | No: Redis is internal, and its access control (ACLs) cannot practically limit each agent to its own sandboxes; the node it names is usually not reachable from outside anyway |
| Query-only replicas alone (they exist already) | Lookups keep working, but creates, lists and `GET /nodes` still fail |
| Leader election in a sidecar container | Stopping on a lost lease would be enforced only from outside, and it is one more image |
| Leader election built into the scheduler (chosen) | `client-go` is already a dependency, and stopping on a lost lease is reliable only inside the process |
| Static machines and Docker Compose | Need a Redis lock and an address that follows the leader, such as a virtual IP |

#### Behavior after the fix

Expected:

- Losing the scheduler's machine costs about 15 s instead of about 5
  minutes.
- Existing sandboxes stay reachable meanwhile.

**Proofs:** [before the fix](proofs/03-high-availability/current.md), by hand.

### 4. High availability (Redis): if Redis is down, agents cannot reach their sandboxes

**Status:** approach suggested, not built. Problem reproduced. One related
fix is built (Go, scheduler): heartbeats no longer fail when Redis is down.

#### Problem

With Redis configured (query-only schedulers and section 3's design need it),
one Redis, at one address, holds the bindings and the `Idempotency-Key`
replies:

- While it is down, no agent can use a sandbox it already has.
- Before the heartbeat fix, after about a minute no agent could start one
  either: when the scheduler could not write a node's changed sandbox list to
  Redis, it failed that node's heartbeat, as upstream does. The node treats
  that like an unreachable scheduler and waits 5, 10, 20, 40, then 60 s
  between attempts, so its heartbeats ended up more than 30 s apart and it
  was marked
  `unhealthy`.
- A failover to a replica can lose the newest writes, since replicas copy
  them asynchronously. Bindings come back from heartbeats, within 10 minutes
  when only some are lost (section 10's marker key survives such a loss).
  `Idempotency-Key` replies do not, so a retry across the failover can make
  a second sandbox. Found in the code.

Redis stopped for 2 minutes in the lab, with two sandboxes created meanwhile:

- At once, every call on an existing sandbox failed with
  `503 binding store unavailable`, and every create with an
  `Idempotency-Key` with `503 idempotency store unavailable`. Creates without
  a key still worked.
- From 77 s on, the nodes flapped between `ready` and `unhealthy`; at 127 s
  both were down and every create got `503 no ready nodes`.
- Once Redis was back, it took 14 to 21 s until every sandbox answered and
  both nodes were `ready`: each node first waited out its retry delay.

#### Solution

- **Heartbeats do not depend on Redis (built):**
  - The scheduler answers OK when the binding write fails, and the next
    heartbeat retries it, since a list's fingerprint is saved only after a
    successful write (section 10).
  - A Redis outage then costs only calls on existing sandboxes, not nodes or
    creates.
  - The reply still carries the shared CPU features: the scheduler sends
    them to each node only once, so a failed heartbeat used to lose them for
    good.
- **Failover:** a replica that takes over by itself, with Redis Sentinel
  (Redis's own monitor, which promotes a replica when the primary fails) or
  a managed Redis.
  - The scheduler and the gateway take one Redis address each, so use a
    managed Redis whose address follows the primary, or add Sentinel
    settings and the Go Redis client's failover client.
- **`Idempotency-Key` replies kept apart:** they cannot be rebuilt from the
  nodes, unlike bindings, so:
  - They get their own Redis that saves to disk.
  - The gateway waits until a replica has a claim (`WAIT 1`) before sending
    the create. That narrows the window in which a failover loses a claim,
    but does not close it: `WAIT` does not make Redis strongly consistent
    ([WAIT](https://redis.io/docs/latest/commands/wait/)).
  - The two Redis addresses are separate settings already
    (`scheduler.redis_addr`, `gateway.idempotency_redis_addr`), so this
    needs no code.

##### Approaches considered

| Question | Chosen, and why |
| --- | --- |
| A heartbeat when Redis is down | Accept it and retry the write at the next heartbeat. Failing it, as before, makes healthy nodes look dead and turns a Redis outage into a create outage |
| How Redis fails over | Sentinel or a managed Redis. Redis Cluster is not needed: the data is small, and sharding does not remove the asynchronous copy |
| Where `Idempotency-Key` replies live | Their own Redis, saving to disk, with `WAIT`. A database such as PostgreSQL would be durable, but adds a second kind of store for one feature |

#### Behavior after the fix

The same 2-minute outage with the heartbeat fix: both nodes stayed `ready`,
creates without a key kept working, and once Redis was back everything
answered within 2 s (before: 14 to 21 s). Calls on existing sandboxes and
creates with an `Idempotency-Key` still fail while Redis is down; failover
(not built) would shorten that.

**Proofs:** [before the fix](proofs/04-redis-high-availability/current.md),
[after the heartbeat fix](proofs/04-redis-high-availability/post-fix.md),
[script](proofs/04-redis-high-availability/scripts/reproduce-redis-outage.py).

### 5. Fault tolerance (failure detection): a dead node keeps getting new sandbox requests

**Status:** implemented (Go, scheduler).

#### Problem

- Nodes send a heartbeat every 5 s; the scheduler marks a node `unhealthy`
  after 30 s without one (`report_ttl`), and a node that stops cleanly
  unregisters.
- But with static discovery, placement ignored this: `Schedule` picked from
  every node in the config file.
  With 2 nodes, every other create went to a crashed, stopped or silent node
  and failed with `502`, although the other node was fine.

#### Solution

- Place new sandboxes only on nodes with a heartbeat within `report_ttl`,
  the `ready` ones.
- With none, refuse at once with `503 no ready nodes`, so a retry is safe.
- Local runs must now turn on heartbeats
  ([services/README.md](../services/README.md), "Node readiness").

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Skip only `unhealthy` nodes | Not enough: a crashed node stays `ready` for 30 s, and an unregistered node looks "never heard from", not `unhealthy` |
| `ready` only, but all nodes when none is `ready` (the first version) | Dropped: when every node was dead, it still sent creates to them |
| `ready` only, `503` when none (chosen) | A clear error at once; the gap right after a scheduler restart is covered in section 6 |

#### Behavior after the fix

- A node that is down, silent or unregistered gets no creates; they all go to
  the live node.

Known limits:

| Case | What happens |
| --- | --- |
| Creates within 30 s of a crash | The node still counts as `ready`; creates that cannot connect to it (refused or timed out) are retried on another node (section 6) |
| Every node cut off from the scheduler | `503`, although the nodes still serve |

**Proofs:** [before the fix](proofs/05-failure-detection/current.md),
[after the fix](proofs/05-failure-detection/post-fix.md),
[script](proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py).

### 6. Retry (fault tolerance): creates sent to a dead node fail, although another node is up

**Status:** implemented (Go, gateway and scheduler).

#### Problem

- After a crash, the scheduler keeps a node `ready` until its last heartbeat
  is 30 s old (section 5), and the gateway answered `502` without trying
  another node: for up to 30 s, every other create (round-robin) failed,
  although another node was up.
- After a scheduler restart, no node counted as ready until its first
  heartbeat: each restart refused all creates for about 3.5 s.

#### Solution

- **Retry:** only when the gateway could not connect to the chosen node: it
  refused, or did not answer within 2 s, as when its machine is off. Nothing
  reached it, so the retry cannot make a duplicate.
  - The gateway asks the scheduler again, excluding that node, and sends the
    create there: at most once, for create bodies up to 64 KiB.
  - Connections to nodes now time out after 2 s instead of 30 s.
  - In Go, a connect timeout can look like the request's deadline, so the
    failed connect is checked first.
- **Startup grace:** for the first `report_ttl` (30 s) after the scheduler
  starts, it also uses nodes it has not heard from yet; once a node has
  reported, only its heartbeats count.

The scheduler API gains `exclude_node_ids`; no Rust code builds a
`ScheduleRequest`, so the node is not affected.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Retry on any failure (dropped connection, timeout, `5xx`) | No: the node may already have made the sandbox, so a retry could duplicate it (section 8) |
| Retry only when the connection could not be opened: refused or timed out (chosen) | Safe: nothing was sent |
| Restart gap: a startup grace in the scheduler (chosen) | Go only; a dead node picked meanwhile costs one fast retry |
| Restart gap: cap the node's heartbeat retry wait | Not done: it needs a Rust change. Still useful, see the known limits |

#### Behavior after the fix

- Right after node-b crashed, while still listed as `ready`, every create
  reached node-a and none failed.
- With node-b's address pointing where connects time out (a machine that is
  off), every create reached node-a, 2 s later; the earlier build answered
  `504` to 1 of 3.
- A scheduler restart now refuses creates for about 1 s (the gateway
  reconnecting), down from 3.5 s.

Known limits:

| Case | What happens |
| --- | --- |
| The node accepts the connection and then drops it (Lima's port forward for about 2 s after a crash, or a node dying mid-create) | Still `502`, no retry, since the node may have the create. With an `Idempotency-Key` (section 8) the client can retry safely |
| A scheduler outage longer than about 40 s | Nodes then retry their heartbeat only every 40 to 60 s, longer than the 30 s grace, so creates can get `503` for up to 30 s more. Fix, not built: cap the node's retry wait at 10 s |

**Proofs:** [before the fix](proofs/06-retry/current.md),
[after the fix](proofs/06-retry/post-fix.md),
[script](proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py)
(section 5's, with `--mode crash-now`),
[connect-timeout script](proofs/06-retry/scripts/reproduce-connect-timeout.py).

### 7. Atomic capacity check (admission control): parallel creates overfill a node

**Status:** implemented (Go, scheduler and gateway), for one
scheduler placing sandboxes, the only supported setup today.

#### Problem

Parallel creates did not check capacity atomically: each saw the same free
room, so together they could put more sandboxes on a node than its limit
allows, and it could run out of memory or CPU.

#### Solution

The scheduler remembers what it has promised, and checks capacity and
reserves it in one step under one lock, so creates arriving together cannot
take the same free slot:

- A reservation is 1 slot, plus vCPUs and memory when the create names them
  (`/sandboxes-cold`). The create counts itself, and starting sandboxes
  count.
- The gateway releases the reservation at once when the node rejects the
  create or cannot be connected to. It keeps it when the connection dropped
  after the create was sent, since the node may have made the sandbox.
- A reservation ends when a heartbeat lists the sandbox, after 2 heartbeats
  without it (deleted), or after 10 minutes.
- When every ready node is full: `503 no node has capacity`.
- The first live run found a leak: a sandbox listed before the gateway
  recorded it, then deleted, kept its slot. The scheduler now remembers each
  node's last list and ends such reservations at once.

The scheduler API gains `reservation_id` and a `ReleaseReservation` call.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Heartbeats more often | Narrows the window; does not close it |
| Reservations in the scheduler's memory, under one lock (chosen) | Atomic within one scheduler, which does all placement (section 3) |
| Check-and-reserve in Redis, as one Lua script | Not now. If several schedulers ever place sandboxes on the same nodes, use this:<br>• Reservations move to Redis; one Lua script checks free capacity and reserves it in one step.<br>• Everything else the leader learns from heartbeats moves too, since a node's heartbeat reaches only one scheduler: each node's usage and readiness, ending reservations (with a heartbeat number, so a late, older heartbeat cannot undo a newer one), and the CPU features all nodes share. |

#### Behavior after the fix

- No node goes over its limit; extra creates are refused before reaching a
  node.

Known limits:

| Case | What happens |
| --- | --- |
| Limits not configured | Nothing is checked. No shipped config sets `node_resource_limit`; the lab does: 8 sandboxes, vCPUs up to 200% and memory up to 100% per node |
| Template creates, forks, resumes | A template create reserves only a slot, not its vCPUs and memory; forks and resumes reserve nothing |
| The gateway crashes, or the connection drops after the create was sent | The slot stays reserved for up to 10 minutes |
| The scheduler just started (its 30 s startup grace, section 6), or a new leader took over (section 3) | • Reservations from before are lost.<br>• A node that has not sent a heartbeat yet has no numbers, so its capacity is not checked and creates go to it freely until its first heartbeat, normally within 5 s: a burst in that window can take it over its limit.<br>• From that heartbeat on, its counts plus the reservations made meanwhile are checked.<br>• Fix, not built: cap the creates sent to a node that has not reported yet. |
| Until the gateway records a new sandbox, usually under a second | It may be counted twice, so a create that would fit can be refused; never one too many |
| An older gateway with the new scheduler | Rejected creates hold their slot for 10 minutes; upgrade both |
| A large cluster | • Each create looks at every node and, under the lock, adds up every pending reservation.<br>• Measured on one laptop (no network): one scheduler places about 17,000 creates per second with 100 nodes, 2,400 with 1,000 and 480 with 5,000; the Redis write adds little.<br>• Fix, not built: keep running totals per node, and check only 2 random nodes per create. |

**Proofs:** [before the fix](proofs/07-admission-control/current.md),
[after the fix](proofs/07-admission-control/post-fix.md),
[script](proofs/07-admission-control/scripts/reproduce-capacity-overfill.py),
[benchmark](proofs/07-admission-control/scripts/leader_throughput_bench_test.go).

### 8. Idempotency (safe retries): retrying a create can make a duplicate sandbox

**Status:** implemented (Go, gateway), and tested through the gateway.

#### Problem

If the reply to a create is lost (connection reset, client timeout), the
agent cannot tell whether the sandbox was made, so it retries, and the retry
is a new create:

- A second sandbox is made, possibly on another node.
- The first is orphaned: the agent never learns its ID, and unless the create
  set `autoPause: false`, its timeout only pauses it, so it never goes away
  by itself.

#### Solution

The client sends the same `Idempotency-Key` header on every retry of one
create; the gateway answers a repeat with the first create's reply instead
of creating again. For `POST /sandboxes`, `/v2/sandboxes` and
`/sandboxes-cold` with the header:

- The gateway claims the key in Redis (`SET NX`) before forwarding the
  create.
- On success it saves the reply (5 days by default), and a retry gets it
  back, marked `Idempotent-Replayed: true`.
- A failed create frees the key.
- The same key with a different body gets `422`.

Creates without the header work as before. The keys live in Redis, so this
holds across gateway restarts and several gateways. Settings:
`gateway.idempotency_redis_addr` and `gateway.idempotency_key_ttl`.

##### Approaches considered

| Approach | Pros | Cons |
| --- | --- | --- |
| The client names the sandbox (`PUT /sandboxes/{name}`, Azure Resource Manager style) | Safe to retry by design | Every create needs a unique name the agent must invent and track; changes the E2B API |
| Idempotency key | One extra header | A key reused by mistake for a different create gets the old sandbox back. Needs Redis |
| **Idempotency key + request hash (chosen)** | Same, and a reused key is caught with `422` | A retry must resend exactly the same body |

#### Behavior after the fix

A retry with the same key gets the first sandbox back, and nothing new is
made.

Not covered:

- **The gateway crashes mid-create:** the key stays "in progress" for
  `request_timeout` + 1 minute; retries wait, get `409`, and after that make a
  second sandbox. Fix, not built: save with the key which node got the
  create, have the node record the key on the sandbox, and ask that node
  before creating again.
- **The gateway times out while the node is still creating:** the key is
  freed, so a retry can make a second sandbox. The same fix covers it.
- A Redis restart or failover that loses the keys (section 4), creates sent
  straight to a node, and retries after the key expired.

**Proofs:** [before the fix](proofs/08-idempotency/current.md),
[after the fix](proofs/08-idempotency/post-fix.md),
[script](proofs/08-idempotency/scripts/reproduce-lost-create-response.py).

### 9. Resilience (cancellation safety): one cancelled restore breaks a snapshot on its node until the server restarts

**Status:** implemented (Rust, node). An upstream pull request
reports the same bug from production and fixes it the same way; it is still
open, with changes requested.

#### Problem

- Launching a sandbox from a snapshot first copies the snapshot's files into
  a cache on the node (with S3, a download).
- The download runs inside the HTTP request, so it is cancelled when the
  caller hangs up.
- Callers that need the same file share one download through a "download in
  progress" entry, which only the end of the download removed, and a
  cancelled download never ends:

```text
launch 1   writes "vm_state.bin: download in progress" and starts the download
           the client hangs up -> node-a cancels launch 1 -> the entry stays; nobody will finish it
launch 2   finds the entry, waits for it, fails at once: "ended without a result"
launch 3   the same, and so does every later launch of this snapshot on node-a, until its server restarts
```

Only that snapshot on that node breaks. No client has to misbehave: the
gateway cancels creates after `gateway.request_timeout` (30 s by default), so
any restore slower than that is enough; with S3, the window is the whole
download.

#### Solution

In the node's snapshot cache:

- A caller that finds a download that ended without a result removes that
  entry, only if it is still the same download, never a newer one, and then
  downloads the file itself or waits for whoever started first.
  `pin_local_file`, which waits on the same entries, does the same.
- A finished download removes its entry before publishing the result.
- Every download goes to a hidden temporary file next to the target and is
  renamed into place when complete, so a cancelled download never leaves a
  half-written file that a retry takes as cached. P2P downloads (off by
  default) wrote straight into the target; the upstream review found this.
- Temporary files a crash left behind are removed when the server starts,
  before anything downloads.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Load the snapshot inside the background task the orchestrator already runs for the rest of a create (`run_cancellation_safe`), so a hang-up cannot cancel it | Not done: it fixes only this one caller, though it would also keep the half-done download |
| Fix the cache itself (chosen) | Covers every caller of the cache |

#### Behavior after the fix

- After a cancelled launch, the next launches of the snapshot work at once
  (1.1 s and 0.6 s), with no restart.
- The cancelled download itself is lost and starts again with the next
  launch.
- Launches already waiting at that moment are covered by a unit test only.
- A crash in the middle of a download leaves a hidden `.partial-` file; the
  next server start removes it.

**Proofs:** [before the fix](proofs/09-cancellation-safety/current.md),
[after the fix](proofs/09-cancellation-safety/post-fix.md),
[script](proofs/09-cancellation-safety/scripts/reproduce-cancelled-restore.py).

### 10. Reconciliation (efficiency and correctness): every heartbeat rewrote all bindings in Redis, which expired after 30 s

**Status:** implemented (Go, scheduler, and the Kubernetes manifests). The Kubernetes part is unit-tested only.

#### Problem

- Every 5 s each node sends the IDs of all its sandboxes.
- With Redis configured (optional; no shipped deploy runs it), the scheduler
  keeps each binding (sandbox → node) there. On every
  heartbeat it rewrote the binding of every listed sandbox with a new 30 s
  expiry, and deleted the ones no longer listed: one Redis script with a GET,
  SET and SADD per sandbox.
- **Load grows with sandboxes, not changes:** a binding rarely changes
  between create and delete. It changes only when:
  - the sandbox is restored on another node after its machine dies (section 2);
  - in Kubernetes, the AgentENV pod on a machine is replaced, for example in
    an upgrade.
- **Bindings lived only 30 s:** when a node's heartbeats stopped for 30 s,
  the gateway answered `404` for sandboxes that were still running.

#### Solution

- Keep the full list in every heartbeat, since it repairs the table by
  itself.
- Write to Redis only when the list's fingerprint (a SHA-256 hash of the
  sorted list) changes, and then only the bindings that differ. This saves:
  - commands and network round trips between the scheduler and Redis;
  - Redis writes;
  - the replication traffic those writes cause.
- Four rules keep the fingerprints correct:
  - The fingerprint is saved only after Redis accepted the update, so a
    failed write is retried at the next heartbeat.
  - Applying a list also removes the node's sandboxes that are no longer
    listed, but only where the binding still names this node, so a sandbox
    that moved to another node keeps its binding.
  - Redis can lose its data (a restart without persistence, or a failover
    to an empty replica) while the fingerprints say every list is applied.
    So the scheduler keeps a marker key in Redis and checks it about every
    5 s with one `SETNX`; when it is gone, every node's next list is
    applied again.
  - Every 10 minutes an unchanged list is applied anyway, so bindings lost
    from Redis come back.
- Keep bindings for a year (`scheduler.binding_ttl`; nothing renews it). A
  binding names the node, not its address; a node that unregisters keeps its
  bindings; a binding to an unknown node answers `503 node unavailable`.
- In Kubernetes, name each node after its machine (`spec.nodeName`), not its
  pod. AgentENV runs one pod per machine (a DaemonSet), so the name stays
  unique and survives a new pod; `LookupNode` returns the node's current pod
  IP.

The node code is unchanged.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| Send the list only when a node starts (an intermediate version) | Dropped:<br>• No reconciliation: if a binding write fails (the node's list at its start, or the record after a create), nothing repairs it until the node restarts.<br>• A deleted sandbox keeps its binding, so every request for it goes to its node (data plane) instead of the control plane answering `404` itself. |
| A fingerprint of each list in the scheduler, and bindings that do not expire (chosen) | Scheduler only; still repairs everything within 5 s |

#### Behavior after the fix

- An unchanged list costs no Redis command (before: 2 sandboxes cost 12 SETs
  every 30 s); the data-loss check adds one `SETNX` about every 5 s for the
  whole scheduler.
- Bindings survive scheduler and node restarts.
- A deleted sandbox's binding goes within about 5 s.
- Late heartbeats no longer make running sandboxes return `404`.
- When Redis lost the scheduler's keys, the sandbox answered again 5 s later;
  the earlier build still answered `404` after 60 s (up to 10 minutes).

Not covered:

- With the in-memory store, a scheduler restart loses bindings for up to 5 s.
- Bindings of a machine removed for good stay until they expire; query-only
  schedulers still use the address saved in the binding; the DaemonSet does
  not pin `maxSurge: 0`.

**Proofs:** [before the fix](proofs/10-reconciliation/current.md),
[after the fix](proofs/10-reconciliation/post-fix.md),
[scripts](proofs/10-reconciliation/scripts/).

### 11. Graceful degradation (partial results): one node down makes the list of all sandboxes fail

**Status:** implemented (Go, gateway).

#### Problem

- To list sandboxes (`GET /sandboxes`, `GET /v2/sandboxes`), the gateway
  asks every node and merges the answers.
- If one node did not answer, the gateway threw the other answers away and
  returned `502 cluster list unavailable`, a rule upstream documented on
  purpose ("strict all-or-nothing"). So one stopped, crashed or unreachable
  node made every list fail for everyone: the SDKs' list call, dashboards,
  and agents that find their sandboxes by label, more often as nodes are
  added.
- With node-b's server stopped, both lists answered `502`, although node-a
  had a running sandbox.

#### Solution

- Return what the nodes that answered sent. A node that cannot be reached,
  times out or answers with a 5xx is left out, logged
  (`cluster sandbox list left out a node`), and named in the
  `x-agentenv-unavailable-nodes` header (comma-separated node IDs).
- The list fails with `502` only when no node answers.
- A 4xx from any node is still returned as is, since the request itself was
  rejected.

##### Approaches considered

| Approach | Verdict |
| --- | --- |
| All or nothing (before) | Never incomplete, but one node down makes every list fail |
| Partial results, missing nodes named in a header (chosen) | • Lists keep working.<br>• A client that needs the whole list, such as a job that deletes what is not listed, checks the header.<br>• Clients that do not read it, such as the SDKs, see the missing node's sandboxes as gone, which cannot be reached meanwhile anyway.<br>• Elasticsearch does the same: partial results by default, with the failed shards reported. |
| Partial results only when the client asks for them | Safest for old clients, but the default list still fails |
| A gateway setting, off by default | Keeps upstream's behavior for other users; worth adding only to contribute this upstream |
| Leave out only the nodes the scheduler already marks down | A node that just died is not marked for up to 30 s, so the list still fails then |

#### Behavior after the fix

- With node-b's server stopped, both lists answered `200` with node-a's
  sandbox and `x-agentenv-unavailable-nodes: node-b`.
- With node-b back, the full list returned.

Known limits:

- A node that accepts the connection but never answers still holds the list
  until `gateway.request_timeout`, as before.
- While a node is missing, `x-total-running` and each page cover only the
  nodes that answered.

**Proofs:** [before the fix](proofs/11-partial-results/current.md),
[after the fix](proofs/11-partial-results/post-fix.md),
[script](proofs/11-partial-results/scripts/reproduce-dead-node-breaks-list.py).

### 12. Internal managed service

**Status:** approach suggested, not built.

#### Problem

AgentENV serves one group: everyone shares one API key
([env-vars.md](../docs/src/configuration/env-vars.md#L89)), and nothing
records who owns a sandbox. So anyone with the key reaches every sandbox, and
one team can use up the whole cluster.

#### Solution

Split AgentENV into a **control plane**, which decides and keeps records, and
a **data plane**, which runs the sandboxes. Each region has its own copy of
both.

```text
CONTROL PLANE
  each region   gateways ─▶ scheduler (leader + standbys) ─▶ Redis
DATA PLANE
  each region   nodes (machines with KVM, running microVMs) ─▶ S3 bucket
```

**Control plane:**

- **Gateways:** the only entry. Each team gets its own API key; gateways find
  the team from the key and tell the node which team is calling (the API has
  E2B's `X-Team-ID` header, but AgentENV ignores it).
- **Scheduler:** active-passive (section 3). It checks the team's limits in
  the same locked step as a node's room (section 7), and keeps node groups
  for teams with their own machines. A region that outgrows one leader splits
  into cells of about 1,000 machines, each with its own leader: one leader
  places about 2,400 creates per second with 1,000 nodes (section 7).
- **Redis:** with a replica (section 4). It holds the bindings, each team's
  sandbox list and the `Idempotency-Key` replies; all but the replies are
  rebuilt from the nodes.

**Data plane:**

- **Nodes:** machines with KVM: bare metal, cloud VMs with nested
  virtualization, or PVM (experimental, x86_64). Each sandbox, snapshot,
  template and volume gets an owner, and other teams get `404`. Nodes accept
  requests only from the gateway.
- **Every sandbox `secure`:** today only `/v2/sandboxes` makes it so by
  default. Otherwise anyone
  who knows a sandbox's ID, which is in its preview URLs, can run commands in
  it (from the code; not tried live).
- **S3, one bucket per region:** snapshots, templates and volumes, under a
  prefix per team. Disk snapshots are exported there (section 1), so any node
  can restore or resume a sandbox; old snapshots and unused layers are
  deleted.
- **Per-team limits** on template builds (today at most 4 at once per node)
  and on storage.

##### Approaches considered

| Question | Chosen | Why |
| --- | --- | --- |
| One control plane for all regions, or one per region | One per region | A shared one stops every region when it is down |
| Where a sandbox's record lives: a database, or its node | Its node, as today | Redis holds what others need, rebuilt from the nodes |
| How to list a team's sandboxes | A list per team in Redis | One read instead of asking every node; up to 5 s old |
| Where ownership is enforced | The node, told the team by the gateway | Only the node keeps the sandbox's record |
| Where team limits are checked | The scheduler leader | Every create passes through it, so limits stay exact without Redis scripts |

## Setup

All runs in this document used two Lima VMs on a Mac:

- Nodes, each running AgentENV v0.2.4: node-a (VM `agentenv`) on
  `127.0.0.1:8000` and node-b (VM `agentenv-2`) on `127.0.0.1:8001`.
- On the Mac: the gateway (`:8090`), scheduler (`:9090`) and Redis (`:6379`).
- How to build and start the lab, and how to use it: [usage.md](usage.md).
- All changes are on the local branch `local/dev`; nothing has been pushed
  upstream.
