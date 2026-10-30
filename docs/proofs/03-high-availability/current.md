# 3. High availability: current behavior

Proofs for section 3 of [proposal.md](../../proposal.md). Not fixed yet, so there is
no post-fix file.

## Scheduler killed for 20 s, by hand (2026-10-28 02:53 IST)

A sandbox was created through the gateway. Then the scheduler was killed
(SIGKILL) for 20 s and started again:

```text
02:53:57.122 created sandbox 01a17f3f-8585-7ff1-a02e-d5b5d24e3900 through the gateway: HTTP 201, on node-a
02:53:57.147 killed the scheduler (SIGKILL, pid 86103)
02:53:58.150 GET the sandbox through the gateway: HTTP 503 'connection error: desc = "transport: Error while dialing: di'
02:53:58.153 GET the sandbox directly on node-a: HTTP 200 '{"templateID":"docker.io/library/ubuntu@sha256:a8fb2ba881817'
02:53:58.153 create through the gateway (body {}): HTTP 503 'connection error: desc = "transport: Error while dialing: di'
02:53:58.154 GET /nodes through the gateway: HTTP 503 'connection error: desc = "transport: Error while dialing: di'
02:54:18.161 20 s later, GET the sandbox directly on node-a: HTTP 200 '{"templateID":"docker.io/library/ubuntu@sha256:a8fb2ba881817'
02:54:18.162 20 s later, GET the sandbox through the gateway: HTTP 503 'connection error: desc = "transport: Error while dialing: di'
02:54:18.164 started the scheduler again: pid 6066
02:54:21.443 GET the sandbox through the gateway, 3.3 s after the start: HTTP 200 '{"templateID":"docker.io/library/ubuntu@sha256:a8fb2ba881817'
02:54:21.445 create through the gateway (body {}), 3.3 s after the start: HTTP 422 'Failed to deserialize the JSON body into the target type: mi'
02:54:21.700 delete through the gateway: HTTP 204 ''
```

node-a's log (UTC): its heartbeats failed and were retried after 5 s, then
10 s.

```text
2026-10-27T21:23:57.936870Z  WARN agentenv::observability::reporter: observability heartbeat failed error=heartbeat rpc failed: code: 'The service is c
2026-10-27T21:24:02.939947Z  WARN agentenv::observability::reporter: observability heartbeat failed error=heartbeat rpc failed: code: 'The service is c
2026-10-27T21:24:12.949525Z  WARN agentenv::observability::reporter: observability heartbeat failed error=heartbeat rpc failed: code: 'The service is c
```

**Result:** the sandbox ran on node-a the whole time, while every gateway
request (lookup, create, node list) got `503`. Everything worked again 3.3 s
after the scheduler started: sandbox locations are in Redis here, and the
startup grace (section 6) let creates through before the first heartbeats.
