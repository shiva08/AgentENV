# The two-node lab: a step-by-step guide

This guide builds a small AgentENV cluster on one Mac, then walks through a
first sandbox. Every output shown is from this lab, built on 2026-10-26, with
the examples run on 2026-10-29; IDs and times will differ on yours. The work
in [proposal.md](proposal.md) uses this lab.

## What you are building

AgentENV runs **sandboxes**: small Linux computers, each a separate microVM,
that AI agents use to run code safely. You ask for a sandbox over HTTP and get
back its ID. With that ID you can look at the sandbox, pause it, resume it,
save a copy of it, or delete it.

The lab has four kinds of pieces:

| Piece | What it does | Runs on | Address |
| --- | --- | --- | --- |
| node-a and node-b | Run the sandboxes. Each is the `aenv` service in its own Linux VM | Lima VMs `agentenv` and `agentenv-2` | `127.0.0.1:8000` and `127.0.0.1:8001` |
| Scheduler | Picks a node for each new sandbox, and remembers which node has each sandbox | The Mac | `127.0.0.1:9090` |
| Gateway | The one address you call. It asks the scheduler which node to use, and forwards your request there | The Mac | `127.0.0.1:8090` |
| Redis | A small database: the scheduler's "sandbox → node" records, and the gateway's saved replies for `Idempotency-Key` | The Mac | `127.0.0.1:6379` |

How a request travels:

```text
you (curl)
   │
   ▼
gateway :8090 ──── "which node?" ────▶ scheduler :9090 ──▶ Redis :6379
   │                                       ▲
   │ forwards your request                 │ every 5 s, each node says
   ▼                                       │ "I am alive; these are my sandboxes"
node-a :8000  or  node-b :8001 ────────────┘
(VM agentenv)     (VM agentenv-2)
```

Words you will see:

- **Timeout:** how long a sandbox lives, in seconds. When it runs out, the
  sandbox is paused.
- **Pause and resume:** pausing saves a sandbox's memory and disk on its
  node and stops it; resuming starts it again from that point, with the same
  ID.
- **Snapshot:** a saved copy of a sandbox. **Restore** starts a new sandbox,
  with a new ID, from a snapshot.
- **Heartbeat:** the message each node sends the scheduler every 5 s. A node
  silent for 30 s gets no new sandboxes.

## Before you start

- A Mac with Apple silicon that supports nested virtualization (a VM inside a
  VM): Apple documents it for M3 or later, with macOS 15 or later. This lab:
  Apple M5 Pro, macOS 26.5.2.
- About 14 GiB of free memory for the two VMs, and disk space: each VM's disk
  can grow to 60 GiB.
- Run every command from the repository root.

## Part 1: Build the lab (once)

### Step 1: Install the tools on the Mac

```bash
brew install lima go redis jq
```

### Step 2: Create two Linux VMs

Sandboxes are microVMs, and they need Linux with KVM (`/dev/kvm`). On a Mac,
they get it from a Linux VM with nested virtualization:

```bash
limactl start --name=agentenv   --vm-type=vz --arch=aarch64 --nested-virt --cpus=4 --memory=8 --disk=60 --mount-none -y template:ubuntu
limactl start --name=agentenv-2 --vm-type=vz --arch=aarch64 --nested-virt --cpus=4 --memory=6 --disk=60 --mount-none -y template:ubuntu
```

| Option | Meaning |
| --- | --- |
| `--vm-type=vz --arch=aarch64` | Use Apple's own virtualization, with ARM Linux |
| `--nested-virt` | Let the VM run VMs itself; this gives it `/dev/kvm` |
| `--mount-none` | Keep the Mac's files out of the VMs |
| `template:ubuntu` | Ubuntu (26.04 with kernel 7.0 here) |

Check: `/dev/kvm` exists in the VM.

```bash
limactl shell --workdir / agentenv ls -l /dev/kvm
```

```text
crw-rw---- 1 root kvm 10, 232 Oct 29 00:42 /dev/kvm
```

### Step 3: Install AgentENV in both VMs

The repository's install script installs the latest AgentENV release (v0.2.4
here) as the service `aenv`, which starts at every boot. It does not start
it now.

```bash
for vm in agentenv agentenv-2; do
  limactl copy scripts/install.sh $vm:/tmp/aenv-install.sh
  limactl shell --workdir / $vm sudo bash /tmp/aenv-install.sh
done
```

### Step 4: Start node-a

Its first start creates the cluster's API key and the seed for sandbox access
tokens; step 5 copies both to node-b.

```bash
limactl shell --workdir / agentenv sudo systemctl start aenv
```

Check: node-a answers on the Mac. Lima forwards each VM's ports to the Mac,
and `204` means healthy.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
```

```text
204
```

### Step 5: Make the two VMs one cluster

Each node needs a name and the scheduler's address. Both go into the
service's settings file, `/etc/default/aenv`; `host.lima.internal` is the Mac,
as seen from a VM. node-b also needs its own port, 8001, so the two nodes do
not clash on the Mac, and node-a's API key and token seed, so one key works
for both.

```bash
# Settings both nodes share: report to the scheduler on the Mac
SCHED='AENV_OBSERVABILITY_SCHEDULER_ENDPOINT="http://host.lima.internal:9090"'
REPORT='AENV_OBSERVABILITY_SCHEDULER_REPORT_ENABLED="true"'

# node-a's API key and token seed, for node-b
KEY="$(limactl shell --workdir / agentenv sudo cat /var/lib/aenv/secrets/api-key)"
SEED="$(limactl shell --workdir / agentenv sudo cat /var/lib/aenv/secrets/sandbox-access-token-hash-seed)"

# node-a: its name and the scheduler
printf '%s\n' 'AENV_NODE_ID="node-a"' "$REPORT" "$SCHED" | limactl shell --workdir / agentenv sudo tee -a /etc/default/aenv >/dev/null

# node-b: port 8001, its name, the scheduler, and node-a's key and seed
limactl shell --workdir / agentenv-2 sudo sed -i 's|^API_ADDR=.*|API_ADDR="127.0.0.1:8001"|' /etc/default/aenv
printf '%s\n' 'AENV_NODE_ID="node-b"' "$REPORT" "$SCHED" "AENV_API_KEY=\"$KEY\"" "AENV_SANDBOX_ACCESS_TOKEN_HASH_SEED=\"$SEED\"" | limactl shell --workdir / agentenv-2 sudo tee -a /etc/default/aenv >/dev/null
unset KEY SEED

# Apply: restart node-a, start node-b
limactl shell --workdir / agentenv sudo systemctl restart aenv
limactl shell --workdir / agentenv-2 sudo systemctl start aenv
```

Check: both nodes answer, and node-b has its settings.

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8001/health
limactl shell --workdir / agentenv-2 sudo grep -E '^(API_ADDR|AENV_NODE_ID|AENV_OBSERVABILITY_SCHEDULER_(ENDPOINT|REPORT_ENABLED))=' /etc/default/aenv
```

```text
204
204
API_ADDR="127.0.0.1:8001"
AENV_NODE_ID="node-b"
AENV_OBSERVABILITY_SCHEDULER_REPORT_ENABLED="true"
AENV_OBSERVABILITY_SCHEDULER_ENDPOINT="http://host.lima.internal:9090"
```

### Step 6: Build the gateway and the scheduler

```bash
make -C services build
```

Both read [services/config/lima.json](../services/config/lima.json):

- The two nodes' addresses. New sandboxes go to them in turn (`round_robin`).
- Limits per node: at most 8 sandboxes, vCPUs up to 200% of the node's CPUs,
  and memory up to 100% of its RAM. A create that fits on no node gets
  `503 no node has capacity`.

## Part 2: Start the lab (each time)

After a Mac restart, do all three steps; otherwise, start only what is not
running.

### Step 1: Start the VMs, if they are stopped

```bash
limactl start agentenv && limactl start agentenv-2
```

The `aenv` service in each VM starts by itself.

### Step 2: Start Redis, the scheduler and the gateway

One command per terminal, in this order, from the repository root:

```bash
# Terminal 1: Redis. It saves nothing to disk.
redis-server --bind 127.0.0.1 --port 6379 --save "" --appendonly no --dir /tmp

# Terminal 2: the scheduler
services/bin/scheduler -config services/config/lima.json

# Terminal 3: the gateway. It needs the nodes' API key.
AENV_API_KEY="$(limactl shell --workdir / agentenv sudo cat /var/lib/aenv/secrets/api-key)" services/bin/gateway -config services/config/lima.json
```

### Step 3: Check that both nodes report to the scheduler

```bash
KEY="$(limactl shell --workdir / agentenv sudo cat /var/lib/aenv/secrets/api-key)"
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8090/nodes | jq -r '.[] | "\(.id) \(.status)"'
```

```text
node-a ready
node-b ready
```

If a node is missing or not `ready`, wait a minute and check again: a node
that could not reach the scheduler waits longer between tries, up to 60 s.

### Stop, restart or remove the lab

- **Restart Redis:** restart the scheduler too. Redis keeps nothing on disk,
  and the scheduler rebuilds its records from the nodes' heartbeats
  ([proposal.md](proposal.md), section 10).
- **Stop the VMs:** `limactl stop agentenv && limactl stop agentenv-2`.
- **Remove the lab for good:** `limactl delete --force agentenv agentenv-2`.
- **Optional, restores on any node:** a shared S3 snapshot store, with Docker
  ([proofs/02-restore/current.md](proofs/02-restore/current.md),
  "Setup for run 2").

## Part 3: Your first sandbox

Run the steps in order, in one terminal; each uses the IDs saved before it.

### Step 0: Set up the terminal

```bash
export GW=http://127.0.0.1:8090
export KEY=$(limactl shell --workdir / agentenv sudo cat /var/lib/aenv/secrets/api-key)
api() { curl -s -w '%{stderr}HTTP %{http_code}\n' -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -X "$@"; }
```

- `api` is curl with the API key. It prints the HTTP status on a line of its
  own, such as `HTTP 201`, and passes the JSON on to `jq`.
- Keep URLs in quotes: zsh reads `?` as a wildcard.

### Step 1: Create a sandbox

```bash
SBX=$(api POST "$GW/sandboxes-cold" -d '{"image": "ubuntu:22.04", "cpuCount": 1, "memoryMB": 512, "timeout": 600, "metadata": {"owner": "me"}}' | jq -r .sandboxID)
echo $SBX
```

```text
HTTP 201
01a18485-bd18-7410-82d3-dfb2403d84f9
```

- `timeout` is how long it lives, in seconds; the default is only 15 s. When
  it runs out, the sandbox is paused, or deleted if the create set
  `"autoPause": false`.
- The first create from an image downloads the image, which can take tens of
  seconds; later creates took about 7 s here.
- `metadata` is a free label. Here `owner=me` lets you list and delete only
  your own sandboxes.

### Step 2: Look at it

```bash
api GET "$GW/sandboxes/$SBX" | jq '{sandboxID, state, cpuCount, memoryMB, startedAt, endAt}'
```

```text
HTTP 200
{
  "sandboxID": "01a18485-bd18-7410-82d3-dfb2403d84f9",
  "state": "running",
  "cpuCount": 1,
  "memoryMB": 512,
  "startedAt": "2026-10-28T21:58:39.641590662Z",
  "endAt": "2026-10-28T22:08:43.836470666Z"
}
```

`endAt` is when the timeout runs out. Times are in UTC.

### Step 3: List your sandboxes

```bash
api GET "$GW/v2/sandboxes?state=running&state=paused&metadata=owner%3Dme" | jq -r '.[] | "\(.sandboxID) \(.state)"'
```

```text
HTTP 200
01a18485-bd18-7410-82d3-dfb2403d84f9 running
```

- Repeat `state=` for each state you want; `state=running,paused` is
  rejected.
- `metadata=owner%3Dme` (`owner=me`, URL-encoded) keeps only your sandboxes.
- If a node is down, you
  still get the other nodes' sandboxes, and the response header
  `x-agentenv-unavailable-nodes` names the missing node ([proposal.md](proposal.md),
  section 11).

### Step 4: Pause it

```bash
api POST "$GW/sandboxes/$SBX/pause"
api GET "$GW/sandboxes/$SBX" | jq -r .state
```

```text
HTTP 204
HTTP 200
paused
```

A paused sandbox keeps its ID and is saved on its node's disk. It survives a
restart or crash of the node's `aenv` service, but not the loss of the node's
machine ([proposal.md](proposal.md), sections 1 and 2).

### Step 5: Resume it

```bash
api POST "$GW/v2/sandboxes/$SBX/connect" -d '{"timeout": 600}' | jq '{sandboxID}'
api GET "$GW/sandboxes/$SBX" | jq -r .state
```

```text
HTTP 201
{
  "sandboxID": "01a18485-bd18-7410-82d3-dfb2403d84f9"
}
HTTP 200
running
```

- `201`: it was paused and runs again, from where it was paused, with the
  same ID. `200`: it was already running.
- `timeout` can only add time, never shorten it.
- The older `POST "$GW/sandboxes/$SBX/resume"` does the same, but is
  deprecated.

### Step 6: Save a snapshot

```bash
SNAP=$(api POST "$GW/sandboxes/$SBX/snapshots" -d '{}' | jq -r .snapshotID)
echo $SNAP
api GET "$GW/sandboxes/$SBX" | jq -r .state
```

```text
HTTP 201
01a18485-d205-79e2-b381-0a2547e5a18d
HTTP 200
running
```

The sandbox keeps running: a snapshot pauses it only briefly (under 100 ms,
per the README).

### Step 7: Start a new sandbox from the snapshot

```bash
NEW=$(api POST "$GW/v2/sandboxes" -d "{\"templateID\": \"$SNAP\", \"timeout\": 600}" | jq -r .sandboxID)
echo $NEW
```

Here the first try failed and the second, the same two lines again, worked:

```text
HTTP 400
null
```

```text
HTTP 201
01a18485-d2b3-7fd1-9b8b-04fdd6996aa3
```

- The new sandbox starts from the snapshot's memory and disk, under a **new
  ID**.
- `HTTP 400` and `null`: in this lab, each node keeps its snapshots on its own
  disk, and the gateway sent the request to the other node. Run the same two
  lines again; see
  [Things that work differently in this lab](#things-that-work-differently-in-this-lab).
- The older `POST "$GW/sandboxes"` with the same body works too, but is
  deprecated.

### Step 8: Clean up

Delete both sandboxes, then the snapshot:

```bash
api DELETE "$GW/sandboxes/$SBX"
api DELETE "$GW/sandboxes/$NEW"
api DELETE "$GW/snapshots/$SNAP"
```

```text
HTTP 204
HTTP 204
{"code":404,"message":"snapshot '01a18485-d205-79e2-b381-0a2547e5a18d' not found"}HTTP 404
```

- The snapshot delete reached the node without the snapshot, as in step 7.
  Run that line again; here the second try answered `HTTP 204`.
- Deleting a sandbox keeps the snapshots taken from it.

### Extra: retry a create safely

If a create's answer is lost, a retry could make a second sandbox. Send the
same `Idempotency-Key` with every try, and the retry gets the first sandbox
back ([proposal.md](proposal.md), section 8):

```bash
IK=$(uuidgen)
api POST "$GW/sandboxes-cold" -H "Idempotency-Key: $IK" -d '{"image": "ubuntu:22.04", "timeout": 600, "metadata": {"owner": "me"}}' | jq -r .sandboxID
api POST "$GW/sandboxes-cold" -H "Idempotency-Key: $IK" -d '{"image": "ubuntu:22.04", "timeout": 600, "metadata": {"owner": "me"}}' | jq -r .sandboxID
```

```text
HTTP 201
01a18485-e386-7ad1-9552-14da7215ddba
HTTP 201
01a18485-e386-7ad1-9552-14da7215ddba
```

The same key with a different body gets `422`.

### Extra: delete everything labelled `owner=me`

```bash
for id in $(api GET "$GW/v2/sandboxes?state=running&state=paused&metadata=owner%3Dme" 2>/dev/null | jq -r '.[].sandboxID'); do api DELETE "$GW/sandboxes/$id"; done
```

```text
HTTP 204
```

## Cheat sheet

After step 0 of Part 3:

| To | Command | Success |
| --- | --- | --- |
| Create from an image | `api POST "$GW/sandboxes-cold" -d '{"image": "ubuntu:22.04", "timeout": 600}'` | `201` and a `sandboxID` |
| Look at one | `api GET "$GW/sandboxes/$SBX"` | `200` |
| List | `api GET "$GW/v2/sandboxes?state=running&state=paused"` | `200` |
| Pause | `api POST "$GW/sandboxes/$SBX/pause"` | `204` |
| Resume | `api POST "$GW/v2/sandboxes/$SBX/connect" -d '{"timeout": 600}'` | `201`, or `200` if it was running |
| Delete | `api DELETE "$GW/sandboxes/$SBX"` | `204` |
| Snapshot | `api POST "$GW/sandboxes/$SBX/snapshots" -d '{}'` | `201` and a `snapshotID` |
| Restore from a snapshot | `api POST "$GW/v2/sandboxes" -d '{"templateID": "<snapshotID>", "timeout": 600}'` | `201` and a new `sandboxID`; in this lab, try again on `400` |
| Delete a snapshot | `api DELETE "$GW/snapshots/$SNAP"` | `204`; in this lab, try again on `404` |

## Things that work differently in this lab

**Snapshot requests reach only one node.** Each node keeps its snapshots on
its own disk (`repository_backend = "posix_fs"`). The gateway sends a restore
to the nodes in turn, without checking which node has the snapshot, and sends
`GET /snapshots`, `GET /snapshots/{id}` and `DELETE /snapshots/{id}` to one
node, which sees only its own snapshots. So:

- A restore can fail with `400 template ... not found`. Four restores in a
  row of a snapshot made on node-b (2026-10-28, summarized):

  ```text
  restore 1: HTTP 201, new sandbox 01a17ff9-d7a6-7b30-bbe5-779f112683b9 on node-b
  restore 2: HTTP 400 {"code":400,"message":"template 01a17ff9-d730-7823-9251-5dbd84367aed not found"}
  restore 3: HTTP 201, new sandbox 01a17ff9-dc87-7d60-8bb6-4d8ab13e690a on node-b
  restore 4: HTTP 400 {"code":400,"message":"template 01a17ff9-d730-7823-9251-5dbd84367aed not found"}
  ```

- A snapshot delete can answer `404`, and `GET "$GW/snapshots?sandboxID=$SBX"`
  can return an empty list, although the snapshot exists.

Two ways around it:

- **Shared snapshot store:** point both nodes at one S3 bucket
  ([proofs/02-restore/current.md](proofs/02-restore/current.md),
  "Setup for run 2"). Then any node can restore any snapshot: in run 2
  there, node-a restored a snapshot made on node-b in 2 s.
- **In this two-node lab only:** try again once. With nothing else running,
  the next try goes to the other node.

## When something goes wrong

What the answers mean:

| Answer | Meaning |
| --- | --- |
| `200`, `201`, `204` | Done |
| `400 template ... not found` | In this lab: the snapshot is on the other node; try again |
| `401` | The API key is missing or wrong: run step 0 of Part 3 again |
| `404` | No such sandbox: deleted, or lost when its node's service crashed while it was running ([proposal.md](proposal.md), section 1). For a snapshot in this lab: try again |
| `422` | The request body is wrong, or an `Idempotency-Key` was reused with a different body |
| `502 upstream unavailable` | The sandbox's node is down ([proposal.md](proposal.md), section 2) |
| `503` | The scheduler is down, or no node is ready or has room ([proposal.md](proposal.md), sections 3, 5 and 7) |

Common problems:

| What you see | Likely cause | What to do |
| --- | --- | --- |
| `ls: cannot access '/dev/kvm'` (Part 1, step 2) | No nested virtualization | Check the Mac (M3 or later, macOS 15 or later), and that the VM was made with `--nested-virt` |
| `000` from a node's `/health` | Its `aenv` service is not running | See why: `limactl shell --workdir / agentenv sudo systemctl status aenv`, and the log: `limactl shell --workdir / agentenv sudo journalctl -u aenv -n 50`. For node-b, use `agentenv-2` |
| A node missing from `GET /nodes`, or not `ready` | It does not reach the scheduler | Wait a minute. Then check that the scheduler runs, and the node's settings (Part 1, step 5) |
| `HTTP 000` from `api` | Nothing answers on port 8090: the gateway is not running | Start it (Part 2, step 2) |
