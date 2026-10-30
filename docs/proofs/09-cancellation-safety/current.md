# 9. Cancellation safety: current behavior (before the fix)

Proofs for section 9 of [proposal.md](../../proposal.md). After the fix:
[post-fix.md](post-fix.md).

## How the runs were made

[scripts/reproduce-cancelled-restore.py](scripts/reproduce-cancelled-restore.py)
needs the Python standard library, `limactl`, and Docker with the S3 server
image from [02-restore](../02-restore/current.md) (Silo).

```bash
python3 docs/proofs/09-cancellation-safety/scripts/reproduce-cancelled-restore.py
```

- **Setup:** start an S3 server on 127.0.0.1:9000 and a proxy in front of it
  on 127.0.0.1:9002, and point node-a's snapshot store at S3 through the
  proxy. node-a's config is backed up first and restored at the end.
- **Step 1:** create a sandbox on node-a, save a snapshot S of it to S3,
  delete the sandbox.
- **Step 2:** the proxy holds node-a's download of S's `vm_state.bin`. Launch
  S on node-a and give up after 3 s, as a client does.
- **Step 3:** the proxy stops holding. Launch S twice more.
- **Step 4:** restart node-a's server and launch S again.

Requests go straight to node-a, not through the gateway, so the scheduler
cannot send a launch to node-b. Exit status: 1 = bug reproduced, 0 = the
launches after the cancelled one worked, 2 = setup error. At the end, also
after Ctrl-C or an error, the script deletes its sandboxes and S, restores
node-a's config and removes the S3 server.

## Output before the fix (2026-10-28 06:04 IST)

```text
run 69acc03e8f23: node-a = VM agentenv http://127.0.0.1:8000; S3 server on 127.0.0.1:9000, which node-a reaches through a proxy on 127.0.0.1:9002
Each launch: POST /sandboxes on node-a with templateID S, label repro-run=69acc03e8f23.

== Setup
[06:04:53] started the S3 server (Docker container aenv-silo-repro) with bucket aenv-repro-cancel, and the proxy
[06:04:54] node-a keeps snapshots in S3 now, through the proxy (repository_backend = "oss"); its config is backed up in /var/lib/aenv/config/config.toml.repro-cancel-backup

== Step 1: a snapshot S in S3
[06:05:03] created sandbox 01a17fee-78a7-74e0-a2e6-ce6d0fd0768a, saved snapshot S = 01a17fee-8c0d-7181-abb6-496b4424c0c7 to S3, deleted the sandbox

== Step 2: the client gives up while node-a downloads S
[06:05:03] the proxy now holds node-a's downloads of vm_state.bin
[06:05:06] launch S, client timeout 3 s: no answer, the client gave up after 3.0 s
  proxy,  0.0 s after the launch: node-a is downloading vm_state.bin from S3; the proxy holds the answer
  proxy,  3.0 s after the launch: node-a closed the held download: it stopped waiting for it
[06:05:09] the proxy no longer holds anything; sandboxes on node-a from this run: none

== Step 3: launch S again, with nothing held
  launch 1: HTTP 500 after 0.0 s: "artifact not found: vm state artifact for snapshot '01a17fee-8c0d-7181-abb6-496b4424c0c7': inflight cache materialization for 'artifacts/01a17fee-8c0d-7181-abb6-496b4424c0c7/vm_state.bin' ended without a result"
  launch 2: HTTP 500 after 0.0 s: "artifact not found: vm state artifact for snapshot '01a17fee-8c0d-7181-abb6-496b4424c0c7': inflight cache materialization for 'artifacts/01a17fee-8c0d-7181-abb6-496b4424c0c7/vm_state.bin' ended without a result"

== Step 4: restart node-a's server, then launch S
[06:05:10] restarted node-a's server (systemctl restart aenv)
  launch: HTTP 201 after 1.2 s, sandbox 01a17fee-abf9-7961-be86-9f8864206925 (deleted)

[06:05:11] cleanup: delete snapshot 01a17fee-8c0d-7181-abb6-496b4424c0c7: HTTP 204
[06:05:12] cleanup: restored node-a's config and restarted its server
[06:05:12] cleanup: removed the S3 server

== Result
BUG REPRODUCED: after one cancelled launch, 2 of 2 later launches of S on node-a failed; S launched again only after node-a's server restarted.
```

A first run at 05:49 gave the same result. node-a dropped the held download
the moment the client gave up (3.0 s). After that, every launch of S failed
within milliseconds, although nothing was held anymore.
