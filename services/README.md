# services

Go implementation of a distributed Gateway and pluggable Scheduler for AgentENV.

## Features

- Gateway routes control-plane requests by real-time scheduling.
- Gateway aggregates `GET /sandboxes` and `GET /v2/sandboxes` across all scheduler nodes.
- Gateway aggregates `GET /nodes` across all observed nodes in the scheduler.
- Gateway resolves `GET /nodes/{id}` via scheduler and proxies to the target node.
- Gateway routes sandbox requests by existing sandbox-to-node binding.
- Scheduler exposes gRPC API and supports pluggable strategy providers.
- Built-in strategies in v1: round_robin and random.
- Scheduler supports both static node configuration and Kubernetes EndpointSlice discovery.
- Scheduler sandbox binding store can be in-memory or Redis-backed.
- Scheduler can run as a primary read/write service or as query-only replicas that serve only `LookupNode` from Redis.
- Scheduler observes node health from heartbeats. A sandbox-to-node binding is written once at create; a node's sandbox list, sent in every heartbeat and applied only when it changed, adds missing bindings and removes deleted ones.
- HTTP and WebSocket forwarding.
- Optional gateway HTTPS with manual certificates or CertMagic automatic issuance
  and renewal. Local HTTP remains the default. See [Gateway HTTPS](#gateway-https).

## Header compatibility

Gateway treats these headers as sandbox-routing markers:

- x-agentenv-sandbox-id
- e2b-sandbox-id

If one of them exists, gateway resolves node from scheduler binding and forwards request there.

When `gateway.sandbox_proxy_domains` is configured, gateway also accepts host-based sandbox
data-plane URLs in the form `{port}-{sandboxID}.{proxy_domain}`. The host-derived
sandbox ID and port take precedence over conflicting routing headers; the gateway logs
that conflict at debug level and forwards the request to the backend node's `/proxy`
endpoint. Host-based routing requires the sandbox ID to be RFC 952/1123 DNS-label compatible
(`[a-z0-9]([a-z0-9-]*[a-z0-9])?`), and the full `{port}-{sandboxID}` label must fit
within the 63-character DNS label limit.

Sandbox data-plane routing is host- or header-based. Path-derived sandbox IDs are
only used for sandbox control-plane APIs such as `/sandboxes/{id}/pause`; clients
that proxy sandbox traffic through the gateway must use a sandbox proxy host or a
sandbox routing header.

## Build

Prerequisites:

- Go 1.25+ (see `go.mod`)

Commands (from `services/`):

```bash
make tidy
make proto
make build      # builds gateway, scheduler and the Compose planner
make test       # tests gateway, scheduler and the Compose planner
```

The Compose planner (`bin/aenv-compose-plan`) is not a control-plane service:
each AgentENV server runs it to validate Compose sandbox requests. It reads a
JSON request on stdin, writes the plan JSON to stdout, and exits with status 65
for a Compose project the sandbox cannot run, or 1 for any other failure.

Per-service (from `services/gateway/` or `services/scheduler/`):

```bash
make build
make test
```

### Scheduler throughput benchmark

From `services/`, run:

```bash
go test ./scheduler/internal -run '^$' -bench '^BenchmarkSchedule(Service|GRPC)$' -benchtime=1s -count=3
```

The benchmarks cover direct `Service.Schedule` calls and in-memory gRPC, using
round-robin with 1 or 32 nodes and 1, 2, 4, or 8 workers. Results are reported
as `schedule/s`. Node and connection setup and a warmup call are not timed;
any call error fails the run. The gRPC case includes serialization and handler
work, but neither case includes TCP traffic or sandbox startup.

## Run locally

Start scheduler:

```bash
make run-scheduler
```

Start gateway with the same API key configured on every AgentENV runtime node:

```bash
export AENV_API_KEY="e2b_$(openssl rand -hex 32)"
make run-gateway
```

The default local config uses `127.0.0.1:9090` for the scheduler.

Runtime nodes must send heartbeats to the scheduler
(`AENV_OBSERVABILITY_SCHEDULER_REPORT_ENABLED=true` and
`AENV_OBSERVABILITY_SCHEDULER_ENDPOINT=http://127.0.0.1:9090`); the scheduler
places new sandboxes only on nodes with a recent heartbeat.

The gateway and runtime nodes require the same API key for control-plane APIs.
The gateway reads `AENV_API_KEY` or `/run/secrets/api-key`; it does not generate
a key. The gateway routes data-plane requests without authenticating them
because only the owning runtime has the sandbox policy needed to distinguish
public ingress, private ingress, and secure envd. Private application proxy
requests use the sandbox response's `trafficAccessToken` in the
`e2b-traffic-access-token` header; secure envd requests use `X-Access-Token`.

## Scheduler configuration

Scheduler discovery modes:

- `static` (default): use `scheduler.nodes` from config.
- `kubernetes`: watch EndpointSlices for a headless Service and build the node list from serving Pod endpoints. Terminating endpoints, or Pods matching `no_schedule_pod_selector`, are kept as lingering/no-schedule nodes; Pods matching `ignore_pod_selector` are excluded.

General config notes:

- `scheduler.report_ttl` must be a duration string such as `"30s"` in JSON config files.
- `scheduler.binding_ttl` must be a duration string such as `"8760h"` in JSON config files.
- `scheduler.report_ttl` controls how long an observed node heartbeat stays healthy.
- `scheduler.binding_ttl` (default `8760h`) is how long a sandbox-to-node binding lives after it is written. Heartbeats do not renew bindings, so keep it longer than any sandbox lives; it only clears bindings nobody removed, such as those of a node that is gone for good.
- `scheduler.redis_addr` selects Redis-backed sandbox binding storage when set; when empty, the scheduler uses the in-memory binding store. It accepts either `host:port` or a Redis URL such as `redis://[:password@]host:6379/db`.
- While Redis is down, heartbeats still succeed and the next heartbeat retries the binding write. The scheduler keeps a marker key, `agentenv:scheduler:bindings:present`, and checks it about every 5 s; if Redis lost its data, every node's next heartbeat rewrites its bindings.
- `--query-only` starts a read-only scheduler that supports only `LookupNode`; it requires `scheduler.redis_addr` and does not need node discovery config.
- `scheduler.artifact_store_capacity` controls how many distinct P2P artifact keys the in-memory artifact index keeps before LRU eviction; defaults to `1000000`.
- `scheduler.artifact_lookup_node_limit` controls how many node IDs a P2P artifact lookup returns; values `<= 0` return all matching nodes.
- `SCHEDULER_BINDING_TTL=<duration>` overrides `scheduler.binding_ttl` from the environment.
- `SCHEDULER_REDIS_ADDR=<addr>` overrides `scheduler.redis_addr` from the environment.
- `SCHEDULER_ARTIFACT_STORE_CAPACITY=<count>` overrides `scheduler.artifact_store_capacity` from the environment.
- `SCHEDULER_ARTIFACT_LOOKUP_NODE_LIMIT=<count>` overrides `scheduler.artifact_lookup_node_limit` from the environment.

### Scheduling strategy

`scheduler.strategy` selects the algorithm used to pick a node from the eligible candidate list. Built-in strategies:

| Strategy | Behaviour |
|---|---|
| `round_robin` (default) | Cycles through eligible nodes in stable order |
| `random` | Picks a uniformly random eligible node |

The strategy interface receives `RichNode` values that carry the node identity (ID + endpoint) together with the latest heartbeat `NodeSnapshot` (sandbox counts, CPU, memory, disk metrics). Current built-in strategies ignore the snapshot, but custom strategy implementations can use it for load-aware decisions.

### Node readiness

Before any other filter, the scheduler keeps only `ready` nodes: nodes whose last heartbeat is newer than `scheduler.report_ttl`. Nodes that stopped sending heartbeats, unregistered, or never reported get no new sandboxes. One exception: for the first `scheduler.report_ttl` after the scheduler starts, nodes it has not heard from yet also count, because it has not been listening long enough to miss a heartbeat. When no node is `ready`, `Schedule` fails with `Unavailable` (`no ready nodes`), which the gateway returns as HTTP 503. Runtime nodes must therefore enable heartbeat reporting. After a scheduler restart, creates fail until the first heartbeats arrive.

### Node resource limit

`scheduler.node_resource_limit` defines per-node resource thresholds that are evaluated **before** the strategy runs. Any node whose heartbeat snapshot exceeds a configured limit is removed from the candidate list, regardless of which strategy is in use. This is a generic guard-rail that sits above the strategy layer — strategies only see nodes that already passed the resource filter.

Only `ready` nodes reach this filter. During the startup grace (see "Node readiness"), a node that has not reported yet has no heartbeat snapshot and passes unchecked.

For sandbox creates, the filter also counts the create itself and the creates already placed on the node that its heartbeat does not show yet, so a node at a limit is full. Example: with `max_sandbox_count: 1`, two creates arriving together on an empty node get one sandbox and one `no node has capacity`. When the scheduler picks a node for a create, it reserves a slot there, plus the vCPUs and memory for `POST /sandboxes-cold`. The reservation ends when:

- the gateway reports that the node rejected the create or could not be reached (`ReleaseReservation`);
- the node's heartbeat lists the sandbox, or two heartbeats after the assignment do not list it (it was deleted);
- 10 minutes pass, for example when the gateway crashed or a create timed out.

Reservations are kept in memory, so only one scheduler may place sandboxes. Upgrade gateways together with the scheduler: an older gateway never releases reservations, so a rejected create holds its slot for 10 minutes.

All fields are optional. Omitting a field (or setting the whole block to `null`) disables that particular check.

| Field | Type | Description |
|---|---|---|
| `max_sandbox_count` | uint32 | Maximum total sandbox count, starting sandboxes included |
| `max_sandbox_starting_count` | uint32 | Maximum concurrently starting sandboxes |
| `max_cpu_used_percent` | uint32 | Maximum observed CPU usage (0–100) |
| `max_cpu_allocated_percent` | uint32 | Maximum allocated-CPU-to-physical-CPU ratio; can exceed 100 when overcommit is allowed |
| `max_memory_used_percent` | uint32 | Maximum observed memory usage (0–100) |
| `max_memory_allocated_percent` | uint32 | Maximum allocated-memory-to-physical-memory ratio; can exceed 100 when overcommit is allowed |

Example:

```json
"node_resource_limit": {
  "max_sandbox_count": 50,
  "max_sandbox_starting_count": 10,
  "max_cpu_used_percent": 90,
  "max_cpu_allocated_percent": 150,
  "max_memory_used_percent": 85,
  "max_memory_allocated_percent": 150
}
```

When all nodes are filtered out, the scheduler returns `Unavailable` (`no node has capacity`) to the caller, which the gateway returns as HTTP 503.

## Gateway configuration

- `gateway.scheduler_addr` points to the primary scheduler. The gateway uses it for scheduling, assignment writes, node listing, node detail resolution, and P2P scheduler APIs.
- `gateway.query_only_scheduler_addr` optionally points to a query-only scheduler. When set, sandbox `LookupNode` routing uses this client; when unset, gateway falls back to `gateway.scheduler_addr`.
- `gateway.request_timeout` must be a duration string such as `"30s"` in JSON config files.
- `gateway.request_timeout` bounds scheduler route resolution and regular control-plane requests. Data-plane requests routed by sandbox headers or host-based sandbox domains, streaming requests, and WebSocket connections reuse the client context after routing and are not cut off by this timeout.
- `gateway.forward_response_size` only limits how much of a successful `POST /sandboxes` response the gateway buffers while extracting a sandbox ID for `RecordAssignment`; it is not a global response-size cap for all proxied traffic.
- For sandbox creates (`POST /sandboxes`, `POST /v2/sandboxes`, `POST /sandboxes-cold` with bodies up to 64 KiB), if the gateway cannot connect to the node the scheduler picked (the node refuses, or does not answer within the 2 s connect timeout, as when its machine lost power), the gateway asks the scheduler once more, excluding that node, and sends the create there. It does not retry after a connection was made, because the node may already have the create.
- Cluster list requests (`GET /sandboxes`, `GET /v2/sandboxes`) fan out to every scheduler node and merge results in the gateway. Direct requests to a backend node remain node-scoped.
- A node that cannot be reached, times out, or answers with a 5xx is left out of a cluster list, and the response names it in `x-agentenv-unavailable-nodes` (comma-separated node IDs); the list, `x-total-running` and pagination then cover only the nodes that answered. The gateway fails the list with `502` only when no node answers, and returns a node's 4xx as is, since the request itself was rejected.
- `GET /nodes` returns scheduler-observed node snapshots (including runtime/resource counters), with optional `clusterID` filtering.
- `GET /nodes/{id}` resolves node endpoint via scheduler and then proxies to the runtime node's admin endpoint.
- `GATEWAY_REQUEST_TIMEOUT=<duration>` overrides `gateway.request_timeout` from the environment (for example, `1m30s`).
- `GATEWAY_QUERY_ONLY_SCHEDULER_ADDR=<addr>` overrides `gateway.query_only_scheduler_addr` from the environment.
- `gateway.idempotency_redis_addr` enables `Idempotency-Key` handling for `POST /sandboxes`, `POST /v2/sandboxes`, and `POST /sandboxes-cold`. The first request with a key creates the sandbox; retries with the same key and body get its stored response with `Idempotent-Replayed: true`, a reused key with a different body gets `422`, and a failed create releases the key. Results are kept in Redis for `gateway.idempotency_key_ttl` (default `120h`, 5 days; `GATEWAY_IDEMPOTENCY_KEY_TTL=<duration>` overrides it). It accepts `host:port` or a Redis URL; empty disables it. `GATEWAY_IDEMPOTENCY_REDIS_ADDR=<addr>` overrides it from the environment.
- `gateway.sandbox_proxy_domains` enables host-based sandbox data-plane routing for `{port}-{sandboxID}.{domain}` URLs. Domains are normalized to lowercase, deduplicated, and must be valid DNS names. Sandbox IDs used in host routes must be lowercase RFC 952/1123 DNS labels, and the full `{port}-{sandboxID}` label must be at most 63 characters.
- `GATEWAY_SANDBOX_PROXY_DOMAINS=<domain>[,<domain>...]` overrides `gateway.sandbox_proxy_domains` from the environment.

Logging format defaults to `auto`:

- `auto`: console when stdout looks like an interactive terminal, otherwise JSON
- `console`: force human-readable terminal logs
- `json`: force structured logs for containers and log pipelines

Examples:

```bash
LOG_FORMAT=console make run-scheduler
LOG_FORMAT=json make run-gateway
```

## Gateway HTTPS

Gateway exposes either HTTP or HTTPS through a single listener. Omitting
`gateway.tls` keeps HTTP enabled. Enabling TLS starts only the HTTPS listener;
it does not retain an HTTP listener or redirect HTTP requests. TLS configuration
does not change gateway communication with the internal scheduler or runtime nodes.

### Enable HTTPS

Add a `tls` object under `gateway` in the existing JSON configuration. Merge one
of the following examples into your configuration, keeping other gateway settings.

**Manual certificates**

Use existing PEM certificate and private key files:

```json
{
  "gateway": {
    "https_listen_addr": ":8080",
    "tls": {
      "mode": "manual",
      "cert_file": "/run/gateway-tls/tls.crt",
      "key_file": "/run/gateway-tls/tls.key"
    }
  }
}
```

In Docker, mount certificate and private key files at the configured paths.
The certificate must cover the hostname or IP clients use. Clients
must trust the issuing CA.

**ACME with TLS-ALPN-01**

Automatically obtain and renew a certificate without DNS provider credentials:

```json
{
  "gateway": {
    "https_listen_addr": ":8080",
    "tls": {
      "mode": "acme",
      "domains": ["api.example.com"],
      "storage_dir": "/var/lib/agentenv/tls",
      "challenge": "tls-alpn-01"
    }
  }
}
```

Replace `api.example.com` with a hostname resolving to your gateway, or a public
IP you control. Wildcards are not supported with this challenge. Public TCP port
443 must reach this HTTPS listener; for Docker, publish `443:8080` in addition to
or instead of `8000:8080`. Persist `storage_dir` and make it writable by gateway.

**ACME with DNS-01**

Use Cloudflare DNS validation to obtain certificates for the gateway hostname and
a wildcard sandbox hostname:

```json
{
  "gateway": {
    "http_listen_addr": ":8080",
    "https_listen_addr": ":8080",
    "tls": {
      "mode": "acme",
      "domains": ["api.example.com", "*.sandbox.example.com"],
      "storage_dir": "/var/lib/agentenv/tls",
      "challenge": "dns-01",
      "dns_provider": "cloudflare",
      "dns_config": {"api_token": "<Cloudflare API token>"}
    }
  }
}
```

Replace `api.example.com` with your gateway hostname. Remove
`*.sandbox.example.com` if you do not need wildcard sandbox access.

### Configuration fields

Listener addresses belong to `gateway`; all other fields below belong to
`gateway.tls`.

| Field | Purpose | Requirements, accepted values, and defaults |
| --- | --- | --- |
| `gateway.http_listen_addr` | HTTP listener address in `host:port` format. For example, `:8080` listens on all interfaces on port 8080. | Optional; defaults to `:8080`. Used only when TLS is `off`. Overridden by `GATEWAY_HTTP_LISTEN_ADDR`. |
| `gateway.https_listen_addr` | HTTPS listener address. In a container, this is the container address, not the published host port. | Optional; defaults to `:8080`. Used in `manual` and `acme` modes. Overridden by `GATEWAY_HTTPS_LISTEN_ADDR`. |
| `mode` | Enables TLS and selects the certificate source. | Accepts `off` (default; HTTP only), `manual` (HTTPS with existing certificate and key files), or `acme` (HTTPS with CertMagic generating private keys, requesting certificates from a CA, and renewing them automatically). |
| `cert_file` | Path to a PEM server certificate chain file. | Required in `manual` mode; no default. Must be readable by the gateway process. In containers, use the mounted path. Unused in `acme` mode. |
| `key_file` | Path to the PEM private key matching the server certificate. | Required in `manual` mode; no default. Must be readable by the gateway process. Unused in `acme` mode. |
| `domains` | Names for which to obtain and maintain certificates, such as `["api.example.com"]`. Each entry is a DNS name, a wildcard such as `*.sandbox.example.com`, or a public IP address, without a scheme, port, or path. | Required and nonempty in `acme` mode. Supports multiple DNS names and currently at most one public IP. Wildcards require `dns-01`; IPs require `tls-alpn-01`, so they cannot be combined in one configuration. Private, loopback, and special-purpose IPs are rejected for ACME. Not needed in `manual` mode: the supplied certificate determines the covered names and IPs. |
| `storage_dir` | Directory for CertMagic certificates, private keys, and ACME account data. | Required in `acme` mode; no default. Must be writable and persisted across container recreation. Compose mounts a persistent volume at `/var/lib/agentenv/tls`, but you must still set this field to that path. Unused in `manual` mode. |
| `email` | Contact email supplied to the ACME CA. | Optional in `acme` mode; no email is supplied by default. Not required to enable HTTPS. |
| `ca` | ACME directory URL of the certificate issuer, not a local CA certificate file path. | Optional in `acme` mode; defaults to Let's Encrypt production. A custom value must be an HTTPS URL, and the service must support the requested identifiers and challenge. A test CA directory URL may be used, but its certificates are generally not trusted by clients by default. |
| `challenge` | Method used by the CA to verify control of a domain or IP. | Optional in `acme` mode; defaults to `tls-alpn-01`. Accepts only `tls-alpn-01` or `dns-01`. The former requires public TCP port 443 to reach the gateway HTTPS listener. The latter validates DNS TXT records, supports wildcards and domain deployments without public port 443, and cannot validate IPs. `http-01` is not enabled. |
| `dns_provider` | DNS service provider used for validation. | Required when `challenge=dns-01`. Supports `cloudflare`, `alidns`, `tencentcloud`, and `route53`. Must be omitted or empty for `tls-alpn-01`. |
| `dns_config` | Provider-native JSON object containing credentials and optional settings. | Required when `challenge=dns-01`; see examples below. Use `{}` for Route 53 with AWS SDK credentials supplied outside JSON. Must be omitted for `tls-alpn-01`. Unknown fields and incorrect types are rejected at startup. The whole object is omitted from JSON configuration dumps. |

In `manual` mode, certificate and key files are loaded at startup. There is no
automatic renewal or file reload; restart gateway after replacing them. The
certificate itself determines its validity period and covered names or IPs.

At minimum, `acme` mode requires `mode`, `domains`, and `storage_dir`. Using
`dns-01` additionally requires `challenge`, `dns_provider`, and `dns_config`.

DNS now supports four providers:

| `dns_provider` | `dns_config` | Credentials and optional settings |
| --- | --- | --- |
| `cloudflare` | `{"api_token":"<token>"}` | Requires `api_token`; optionally accepts a separate `zone_token`. |
| `alidns` | `{"access_key_id":"<id>","access_key_secret":"<secret>"}` | Requires both keys; optionally accepts `security_token` for temporary credentials and `region_id`. |
| `tencentcloud` | `{"SecretId":"<id>","SecretKey":"<secret>"}` | Requires both keys; optionally accepts `SessionToken` and `Region`. These names follow the provider's Go fields; `secret_id` and `secret_key` are not accepted. |
| `route53` | `{"access_key_id":"<id>","secret_access_key":"<secret>","region":"us-east-1"}` | Supply both static keys or omit both to use the AWS SDK credential chain (environment, shared profiles, or instance roles). Optional fields include `session_token`, `profile`, and `region`. |

`domains` controls certificate issuance; `gateway.sandbox_proxy_domains` controls
sandbox request routing. Neither setting populates or replaces the other. For
example, wildcard sandbox access requires `*.sandbox.example.com` in `domains`
and `sandbox.example.com` in `sandbox_proxy_domains`.

Clients may use a nonstandard HTTPS port, such as `https://api.example.com:8000`.
However, the CA still connects to public port 443 for `tls-alpn-01` issuance and
renewal; exposing only port 8000 does not satisfy this challenge. Domain deployments
can use `dns-01` instead, while public IP deployments still require forwarding
public port 443 to the HTTPS listener.

## Deploy with Docker Compose

From **repository root**, start gateway + scheduler + two backend nodes:

```bash
# Run scripts/docker-setup.sh first for host prerequisites.
make deploy-up
```

Optional host-based sandbox data-plane routing:

```bash
SANDBOX_PROXY_DOMAINS=sandbox.example.com \
make deploy-up
```

Repository deployment helpers also accept `SANDBOX_PROXY_DOMAINS=<domain>[,<domain>...]`
and pass it to both gateway and runtime node processes.

Check status / logs / teardown:

```bash
make deploy-ps
make deploy-logs
make deploy-down
```

Container deployments use `deploy/docker/config/default.json`, where scheduler service-discovery and backend node endpoints are set for the Docker network.

The compose stack also wires each runtime node for scheduler heartbeat reporting:

- `AENV_NODE_ID` is set per runtime container (`node-a`, `node-b`).
- `AENV_OBSERVABILITY_SCHEDULER_REPORT_ENABLED=true` enables heartbeat reporting.
- `AENV_OBSERVABILITY_SCHEDULER_ENDPOINT` points runtime nodes at `http://scheduler:9090`.
- `SANDBOX_PROXY_DOMAINS`, when set, is passed through as both `GATEWAY_SANDBOX_PROXY_DOMAINS` and `AENV_SANDBOX_PROXY_DOMAINS`.

## Deploy on Kubernetes

From the repository root:

```bash
make k8s-render
make k8s-apply
```

Optional host-based sandbox data-plane routing:

```bash
SANDBOX_PROXY_DOMAINS=sandbox.example.com make k8s-apply
```

The default overlay is `deploy/k8s/overlays/default`.
The make targets materialize a temporary Kustomize build context so Kubernetes runtime nodes always consume the repository's single AgentENV runtime config source: `config/default.toml`.

The DaemonSet injects scheduler-report identity and endpoint wiring for runtime nodes:

- `AENV_NODE_ID` comes from the Kubernetes node name (`spec.nodeName`), the
  same ID the scheduler reads from each EndpointSlice endpoint's `nodeName`.
  Both must change together.
- `AENV_OBSERVABILITY_SCHEDULER_REPORT_ENABLED=true` enables heartbeat reporting.
- `AENV_OBSERVABILITY_SCHEDULER_ENDPOINT` is set to `http://agentenv-scheduler:9090`.
- `AENV_SANDBOX_PROXY_DOMAINS` comes from the shared sandbox proxy ConfigMap.

Shared Kubernetes helpers:

```bash
make k8s-build
make k8s-redeploy
```

For single-machine development, `make k8s-apply-dev` uses the `local-dev`
overlay and mounts the repository `env/` directory directly into the AgentENV
DaemonSet at `/workspace/env`. This avoids copying runtime assets into `/var/lib/agentenv/env`.

For local k3s-style development, use:

```bash
make k8s-load-dev
make k8s-refresh-dev
```

`k8s-load-dev` imports the locally built images into k3s/containerd, while
`k8s-refresh-dev` runs build, load, and rollout restart together.

Deployment model:

- `gateway`: Deployment + ClusterIP Service
- `scheduler`: single-replica Deployment + ClusterIP Service
- `agentenv-node`: privileged DaemonSet with `/dev/kvm` and hostPath `/var/lib/agentenv`
- `agentenv-nodes`: headless Service used by scheduler EndpointSlice discovery

Kubernetes config keys:

- `scheduler.discovery.mode`
- `scheduler.discovery.kubernetes.namespace`
- `scheduler.discovery.kubernetes.service_name`
- `scheduler.discovery.kubernetes.port`
- `scheduler.discovery.kubernetes.scheme` (defaults to `http`)
- `scheduler.discovery.kubernetes.ignore_pod_selector` (optional Kubernetes label selector; matching Pods are excluded from discovery)
- `scheduler.discovery.kubernetes.no_schedule_pod_selector` (optional Kubernetes label selector; matching Pods are kept as lingering/no-schedule nodes)

Kubernetes endpoint address handling:

- Scheduler only accepts EndpointSlice addresses that parse as valid IPs.
- Both IPv4 and IPv6 endpoint addresses are supported.
- IPv6 endpoints are emitted using bracketed host:port form (for example, `http://[2001:db8::10]:8000`).

Operational notes:

- The scheduler uses in-cluster Kubernetes config and watches EndpointSlices plus Pods for service discovery.
- Only serving, non-terminating DaemonSet Pods are schedulable. Use `no_schedule_pod_selector` for drain/no-new-work labels and `ignore_pod_selector` for Pods that should be completely hidden from discovery.
- For the default `memory` binding store, `scheduler` should stay single-replica because sandbox bindings are process-local.
- For high availability, run one primary scheduler with `scheduler.redis_addr` set and multiple query-only scheduler replicas started with `--query-only` against the same Redis. Point gateways at the primary with `gateway.scheduler_addr` and at the query-only service with `gateway.query_only_scheduler_addr`. The primary writes sandbox bindings to Redis while query-only replicas continue to serve data-plane `LookupNode` during primary restarts or upgrades. This HA mode is intentionally data-plane only: requests that proxy to existing sandboxes can keep routing, but control-plane operations that need the primary scheduler, such as creating new sandboxes, scheduling, assignment writes, node listing, node detail resolution, and P2P scheduler APIs, still fail while the primary scheduler is unavailable. Artifact store state is still in-memory and is not covered by this HA mode.
- The gateway is intentionally left as ClusterIP by default; attach an Ingress or LoadBalancer based on your environment.

## gRPC API

Proto contract: api/proto/scheduler.proto

Methods:

- Schedule
- ListNodes
- LookupNode
- RecordAssignment
- ReleaseReservation
- Heartbeat
- ListObservedNodes
- ReportSandboxEvent
- GetNode
- UnregisterNode
