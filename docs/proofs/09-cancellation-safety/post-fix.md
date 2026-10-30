# 9. Cancellation safety: after the fix

Proofs for section 9 of [proposal.md](../../proposal.md). Before the fix:
[current.md](current.md). Same script and steps.

## Unit tests

The Mac has no Rust toolchain, so the checks ran in node-a's VM, in the
v0.2.4 source copy `~/agentenv-src`, whose `artifact_cache.rs` is identical
to this branch's before the change. With only the 3 new tests added, they
failed:

```text
test snapshot::artifact_cache::tests::pin_local_file_ignores_a_fetch_that_ended_without_a_result ... FAILED
test snapshot::artifact_cache::tests::waiter_fetches_itself_when_the_fetch_it_waits_for_is_cancelled ... FAILED
test snapshot::artifact_cache::tests::ensure_cached_fetches_again_after_a_cancelled_fetch ... FAILED
thread 'snapshot::artifact_cache::tests::pin_local_file_ignores_a_fetch_that_ended_without_a_result' (280568) panicked at src/snapshot/artifact_cache.rs:730:67:
called `Result::unwrap()` on an `Err` value: inflight cache materialization for 'runtime/snapshot/image.json' ended without a result
thread 'snapshot::artifact_cache::tests::waiter_fetches_itself_when_the_fetch_it_waits_for_is_cancelled' (280570) panicked at src/snapshot/artifact_cache.rs:711:42:
called `Result::unwrap()` on an `Err` value: inflight cache materialization for 'artifacts/t1/vm_state.bin' ended without a result
thread 'snapshot::artifact_cache::tests::ensure_cached_fetches_again_after_a_cancelled_fetch' (280556) panicked at src/snapshot/artifact_cache.rs:675:14:
called `Result::unwrap()` on an `Err` value: inflight cache materialization for 'artifacts/t1/vm_state.bin' ended without a result
test result: FAILED. 6 passed; 3 failed; 0 ignored; 0 measured; 1027 filtered out; finished in 0.08s
```

With the fix, all 152 snapshot tests pass, the cache tests passed 5 runs in a
row, and `rustfmt --check` and
`cargo clippy -p agentenv --lib --tests -- -D warnings` are clean:

```text
test snapshot::artifact_cache::tests::pin_local_file_ignores_a_fetch_that_ended_without_a_result ... ok
test snapshot::artifact_cache::tests::waiter_fetches_itself_when_the_fetch_it_waits_for_is_cancelled ... ok
test snapshot::artifact_cache::tests::ensure_cached_fetches_again_after_a_cancelled_fetch ... ok
test result: ok. 152 passed; 0 failed; 0 ignored; 0 measured; 884 filtered out; finished in 2.68s
```

## Output after the fix (2026-10-28 06:02 IST)

node-a ran v0.2.4 built from `~/agentenv-src` with only this change
(`cargo build --release -p agentenv --bin server`, 100 s); its original
binary was put back afterwards.

```text
run 42af6b171764: node-a = VM agentenv http://127.0.0.1:8000; S3 server on 127.0.0.1:9000, which node-a reaches through a proxy on 127.0.0.1:9002
Each launch: POST /sandboxes on node-a with templateID S, label repro-run=42af6b171764.

== Setup
[06:02:53] started the S3 server (Docker container aenv-silo-repro) with bucket aenv-repro-cancel, and the proxy
[06:02:54] node-a keeps snapshots in S3 now, through the proxy (repository_backend = "oss"); its config is backed up in /var/lib/aenv/config/config.toml.repro-cancel-backup

== Step 1: a snapshot S in S3
[06:03:03] created sandbox 01a17fec-a46e-7010-accc-d51c85f9188a, saved snapshot S = 01a17fec-b6f9-7dc3-a170-29473cf7dfae to S3, deleted the sandbox

== Step 2: the client gives up while node-a downloads S
[06:03:03] the proxy now holds node-a's downloads of vm_state.bin
[06:03:06] launch S, client timeout 3 s: no answer, the client gave up after 3.0 s
  proxy,  0.0 s after the launch: node-a is downloading vm_state.bin from S3; the proxy holds the answer
  proxy,  3.0 s after the launch: node-a closed the held download: it stopped waiting for it
[06:03:09] the proxy no longer holds anything; sandboxes on node-a from this run: none

== Step 3: launch S again, with nothing held
  launch 1: HTTP 201 after 1.1 s, sandbox 01a17fec-d35c-7870-92e1-68f2e2ad2ea4 (deleted)
  launch 2: HTTP 201 after 0.6 s, sandbox 01a17fec-d785-79e3-924a-89b2a85037b4 (deleted)

== Step 4: restart node-a's server, then launch S
[06:03:11] restarted node-a's server (systemctl restart aenv)
  launch: HTTP 201 after 1.0 s, sandbox 01a17fec-dd29-76f0-8966-430aaec6d7d5 (deleted)

[06:03:12] cleanup: delete snapshot 01a17fec-b6f9-7dc3-a170-29473cf7dfae: HTTP 204
[06:03:13] cleanup: restored node-a's config and restarted its server
[06:03:14] cleanup: removed the S3 server

== Result
PASS: launches of S worked after the cancelled one.
```

## A half-written file (2026-10-30)

The upstream pull request for the same bug is still open: its review found
that after a cancelled download, the retry could take a half-written file as
cached, since the cache trusts any file at the target path. This fix had the
same gap: the P2P download (off by default) writes straight into the target
path. S3 downloads were safe, since they write a temporary file and rename
it. Now the cache downloads every file to a hidden `.partial-<id>` file next
to the target and renames it into place when complete; a cancelled or failed
download leaves nothing at the target.

Before the change, the new test failed: the retry got the 4 bytes the
cancelled download had written (`snap`), not the file:

```text
test snapshot::artifact_cache::tests::ensure_cached_never_reuses_the_partial_file_of_a_cancelled_fetch ... FAILED
  left: [115, 110, 97, 112]
 right: [115, 110, 97, 112, 45, 100, 97, 116, 97]
test result: FAILED. 9 passed; 1 failed; 0 ignored; 0 measured; 1027 filtered out; finished in 0.07s
```

After it, all 153 snapshot tests pass 5 runs in a row, and `rustfmt --check`
and `cargo clippy -p agentenv --all-targets -- -D warnings` are clean:

```text
test result: ok. 153 passed; 0 failed; 0 ignored; 0 measured; 884 filtered out; finished in 1.39s
```

The release build with this change replaced `/usr/local/bin/server.cache-fix`
on node-a, and the script passed again with it (2026-10-30 00:38 IST):

```text
== Step 3: launch S again, with nothing held
  launch 1: HTTP 201 after 1.0 s, sandbox 01a1890f-fb45-7610-ac04-c5be76af711c (deleted)
  launch 2: HTTP 201 after 0.5 s, sandbox 01a1890f-ff2d-7753-8724-5d9e7d3bec51 (deleted)

== Step 4: restart node-a's server, then launch S
[00:38:10] restarted node-a's server (systemctl restart aenv)
  launch: HTTP 201 after 0.9 s, sandbox 01a18910-04a1-7992-9f89-17726baa240a (deleted)

== Result
PASS: launches of S worked after the cancelled one.
```

## Leftover temporary files (2026-10-30)

A crash in the middle of a download leaves its hidden `.partial-` file, and
nothing else names it. The server now removes such files once, when it
starts, before anything downloads; the template builder's cache, built later,
lies below the same root. Before the change the new test did not compile
(`cannot find function remove_partial_files`). After it, all 154 snapshot
tests pass 5 runs in a row, and `rustfmt --check` and
`cargo clippy -p agentenv --all-targets -- -D warnings` are clean:

```text
test snapshot::artifact_cache::tests::remove_partial_files_removes_only_partial_files ... ok
test result: ok. 154 passed; 0 failed; 0 ignored; 0 measured; 884 filtered out; finished in 1.25s
```

## Rerun after the fix

Both nodes still run the original v0.2.4 binary. The fixed build is kept on
node-a as `/usr/local/bin/server.cache-fix`:

```bash
limactl shell --workdir / agentenv sudo sh -c 'install -m 0755 /usr/local/bin/server.cache-fix /usr/local/bin/server && systemctl restart aenv'
python3 docs/proofs/09-cancellation-safety/scripts/reproduce-cancelled-restore.py
limactl shell --workdir / agentenv sudo sh -c 'install -m 0755 /usr/local/bin/server.v0.2.4.bak /usr/local/bin/server && systemctl restart aenv'
```
