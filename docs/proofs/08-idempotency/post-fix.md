# 8. Idempotency: after the fix (with a key)

Proofs for section 8 of [proposal.md](../../proposal.md). Without a key:
[current.md](current.md). Same script, with `--idempotency-key`; the command
in the output shows the script's old path, `scripts/`.

```bash
python3 docs/proofs/08-idempotency/scripts/reproduce-lost-create-response.py --url http://127.0.0.1:8090 --idempotency-key --nodes http://127.0.0.1:8000,http://127.0.0.1:8001
```

## Output, 2 nodes through the gateway (2026-10-27)

With a key, the retry got the first sandbox back and nothing new was made:

```text
$ python3 scripts/reproduce-lost-create-response.py --url http://127.0.0.1:8090 --idempotency-key --nodes http://127.0.0.1:8000,http://127.0.0.1:8001
run 818b5c9ccd70: the agent asks for ONE sandbox from ubuntu:22.04 with Idempotency-Key b40473b2-48bd-4af7-b9da-8988b0da2d7a
proxy: server answered 'HTTP/1.1 201 Created' for sandbox 01a17d2c-763d-71c3-af34-f4ad8166b184; dropping the reply and resetting the connection
agent: attempt 1 got no response after 8s (ConnectionResetError); retrying
agent: attempt 2 succeeded after 0s; sandbox 01a17d2c-763d-71c3-af34-f4ad8166b184 (replayed)
server: 1 sandbox(es) labelled repro-run=818b5c9ccd70: 01a17d2c-763d-71c3-af34-f4ad8166b184
node http://127.0.0.1:8000 holds: 01a17d2c-763d-71c3-af34-f4ad8166b184
node http://127.0.0.1:8001 holds: none
PASS: the retry did not create a duplicate sandbox
cleanup: deleted 1 sandbox(es)
exit=0
```

## Key lifetime

Checked on a later run (24f740e93d03) with the 5-day default:
`redis-cli TTL agentenv:gateway:idempotency:32f491d7-fb6b-4c7d-9c4a-8ebf87e15b6a`
returned `431999` (seconds, about 5 days).
