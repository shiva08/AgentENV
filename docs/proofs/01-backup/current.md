# 1. Backup: current behavior

Proofs for section 1 of [proposal.md](../../proposal.md). Plan only, so there is no
post-fix file. The machine-death case is in
[02-restore](../02-restore/current.md).

## Planned restart and crash on node-a, by hand (2026-10-28 04:43 IST)

Sandboxes were created directly on node-a (`POST /sandboxes-cold`,
`ubuntu:22.04`, 1 vCPU, 512 MiB); the agent's requests went through the
gateway.

Planned restart with two running sandboxes, R1 and R2:

```text
[04:43:04.700]   agent GET R1 through the gateway: 200 state=running
[04:43:04.702]   agent GET R2 through the gateway: 200 state=running
[04:43:05.701] systemctl restart aenv
[04:43:06.230]   agent GET R1 through the gateway: 502 'upstream unavailable'
[04:43:06.231]   agent GET R2 through the gateway: 502 'upstream unavailable'
[04:43:06.534] systemctl restart returned after 0.8 s
[04:43:07.044] node-a answers /health again, 1.3 s after the restart began
[04:43:07.242]   agent GET R1 through the gateway: 200 state=paused
[04:43:07.245]   agent GET R2 through the gateway: 200 state=paused
[04:43:15.734] agent resumes R1 through the gateway: HTTP 201; GET R1: 200 state=running
```

node-a's log: the server paused R1 and R2 one after the other, in about 0.4 s
and 0.25 s:

```text
Oct 28 04:43:05.793358 2026-10-27T23:13:05.793307Z  INFO shutdown:pause_sandbox: agentenv::orchestrator::service: pausing sandbox sandbox_id=01a17fa3-50ad-7f03-
Oct 28 04:43:06.185816 2026-10-27T23:13:06.185795Z  INFO shutdown:pause_sandbox: agentenv::orchestrator::service: pausing sandbox sandbox_id=01a17fa3-6e22-7d52-
Oct 28 04:43:06.434462 2026-10-27T23:13:06.434389Z  INFO shutdown: agentenv::orchestrator::service: orchestrator shutdown completed
```

Crash (`kill -9` of the server process only), with R running and P paused:

```text
[04:43:30.961] paused P: HTTP 204
[04:43:31.127]   agent GET R through the gateway: 200 state=running
[04:43:31.129]   agent GET P through the gateway: 200 state=paused
[04:43:32.186] killed the server process (kill -9), as a crash does
[04:43:32.647]   agent GET R through the gateway: 502 'upstream unavailable'
[04:43:32.648]   agent GET P through the gateway: 502 'upstream unavailable'
[04:43:37.617] systemd started a new server (pid 277897); node-a answers /health 5.5 s after the kill
[04:43:37.700]   agent GET R through the gateway: 404 'sandbox assignment not found'
[04:43:37.703]   agent GET P through the gateway: 200 state=paused
```

Firecracker processes on node-a, before the crash and after the new server
started:

| Process | Before the crash | After |
| --- | --- | --- |
| R's VM, the only large one (57,788 KiB) | Child of the server (pid 277281) | Gone |
| 4 idle warm-pool processes, about 1,750 KiB each | Children of the server | Still running, now children of pid 1: left behind |
| 2 new warm-pool processes | - | Children of the new server (pid 277897) |

## Results

| | Planned restart | Crash |
| --- | --- | --- |
| The agent gets, during the outage | `502` for about 1 s | `502` for about 5 s: systemd waits 5 s before it restarts the server (`RestartSec=5`) |
| Running sandbox afterwards | Paused, same ID; resuming it works | `404 sandbox assignment not found`: lost |
| Paused sandbox afterwards | Not part of this case | Paused, same ID: kept |
| Firecracker processes | - | R's VM died; the old server's 4 idle warm-pool processes kept running. The cleanup killed them, and node-a then created a sandbox normally |

Why, from the code:

- A running sandbox's record lives only in the server's memory
  ([service.rs](../../../src/orchestrator/service.rs#L150-L172)). Only a pause
  writes one to disk, under `$AENV_HOME/persisted-sandboxes`
  ([service.rs](../../../src/orchestrator/service.rs#L1567)).
- A planned stop first pauses every running sandbox, one at a time
  ([service.rs](../../../src/orchestrator/service.rs#L2790)), and systemd allows
  30 s for it ([install.sh](../../../scripts/install.sh#L373-L379)). At 0.25 to
  0.4 s per sandbox, that is about 75 to 120 sandboxes of this size; larger
  sandboxes were not measured.
- A crash leaves no time to pause. The program that serves the VMs' disks
  (the ublk daemon) exits when the server exits
  ([main.rs](../../../storage/ublk-daemon/src/main.rs#L342-L379)). R's VM died
  too; whether the disk program's exit caused it was not checked.
- The new server's first heartbeat no longer listed R, so the scheduler
  removed R's record: `404`, not `502`.
