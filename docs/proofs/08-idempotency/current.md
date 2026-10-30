# 8. Idempotency: current behavior (without a key)

Proofs for section 8 of [proposal.md](../../proposal.md). With the fix:
[post-fix.md](post-fix.md).

## How the run was made

[scripts/reproduce-lost-create-response.py](scripts/reproduce-lost-create-response.py)
puts a proxy between the agent and the gateway. The proxy drops the reply to
the first create and resets the connection, so the agent retries. `--nodes`
shows which node holds each sandbox. Exit status 1 = duplicate made, 0 = no
duplicate. The command in the output below shows the script's old path,
`scripts/`.

```bash
python3 docs/proofs/08-idempotency/scripts/reproduce-lost-create-response.py --url http://127.0.0.1:8090 --nodes http://127.0.0.1:8000,http://127.0.0.1:8001
```

## Output, 2 nodes through the gateway (2026-10-27)

Without a key, the retry made a second sandbox, on the other node:

```text
$ python3 scripts/reproduce-lost-create-response.py --url http://127.0.0.1:8090 --nodes http://127.0.0.1:8000,http://127.0.0.1:8001
run e4829618498d: the agent asks for ONE sandbox from ubuntu:22.04
proxy: server answered 'HTTP/1.1 201 Created' for sandbox 01a17d2c-1430-7511-b10b-33e34dcb3697; dropping the reply and resetting the connection
agent: attempt 1 got no response after 8s (ConnectionResetError); retrying
agent: attempt 2 succeeded after 16s; sandbox 01a17d2c-5597-79a2-a6b9-e1233ec0f6b8
server: 2 sandbox(es) labelled repro-run=e4829618498d: 01a17d2c-5597-79a2-a6b9-e1233ec0f6b8, 01a17d2c-1430-7511-b10b-33e34dcb3697
node http://127.0.0.1:8000 holds: 01a17d2c-1430-7511-b10b-33e34dcb3697
node http://127.0.0.1:8001 holds: 01a17d2c-5597-79a2-a6b9-e1233ec0f6b8
BUG REPRODUCED: one logical create produced 2 sandboxes; the agent only knows about 01a17d2c-5597-79a2-a6b9-e1233ec0f6b8
cleanup: deleted 2 sandbox(es)
exit=1
```
