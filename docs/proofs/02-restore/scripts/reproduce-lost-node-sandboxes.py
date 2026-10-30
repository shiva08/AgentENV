#!/usr/bin/env python3
"""Show that nothing relaunches a dead node's sandboxes or tells the agent.

Uses the two-node Lima setup from docs/usage.md: the gateway on 127.0.0.1:8090,
node-a (VM agentenv) on :8000, node-b (VM agentenv-2) on :8001, and the
scheduler on 127.0.0.1:9090 with its metrics on :9101. Steps:

  1. Through the gateway, create two sandboxes on node-b, A and B. Take a
     snapshot S of A and pause B. A keeps running.
  2. Power off node-b's VM (limactl stop --force agentenv-2), as if the
     machine died.
  3. For --watch seconds, every --every seconds, record: node-b's status in
     the scheduler, the gateway's answers for A and B, the sandboxes on
     node-a, and the scheduler's unhealthy-node gauge. A relaunch of A or B
     would show up on node-a.
  4. Act as the agent: look up A's snapshots through the gateway
     (GET /snapshots?sandboxID=A) and launch S (POST /sandboxes, templateID S).
  5. Start node-b's VM again and ask the gateway for A and B.

Each sandbox is ubuntu:22.04 with 1 vCPU and 512 MiB, labelled
repro-run=<run>. The script deletes them and S at the end, and starts node-b's
VM again if it stopped early. Whether node-a can see S in step 4 depends on the
snapshot repository the nodes use (repository_backend in their config file).

Examples:
  python3 docs/proofs/02-restore/scripts/reproduce-lost-node-sandboxes.py
  python3 docs/proofs/02-restore/scripts/reproduce-lost-node-sandboxes.py --watch 300

Exit status: 1 if nothing relaunched A or B during the watch, 0 if something
did, 2 on setup errors.
"""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time
import urllib.parse
import uuid

# Node ID -> (Lima VM, node API URL as listed in services/config/lima.json).
NODES = {
    "node-a": ("agentenv", "http://127.0.0.1:8000"),
    "node-b": ("agentenv-2", "http://127.0.0.1:8001"),
}
DEAD = "node-b"
SCHEDULER_METRICS = "http://127.0.0.1:9101/metrics"
SCHEDULER_LOG = "/tmp/verify-bindings-scheduler.log"
NODE_CONFIG = "/var/lib/aenv/config/config.toml"
UNHEALTHY_GAUGE = 'agentenv_scheduler_observed_nodes{status="unhealthy"}'


def call(method, url, api_key=None, body=None, timeout=15):
    """Return (HTTP status, body text); status 0 means no HTTP response."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    headers = {"Connection": "close"}
    if api_key:
        headers["X-API-Key"] = api_key
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    try:
        conn.request(method, target, body=data, headers=headers)
        response = conn.getresponse()
        return response.status, response.read().decode(errors="replace")
    except (OSError, http.client.HTTPException):
        return 0, ""
    finally:
        conn.close()


def limactl(*args, timeout=600):
    try:
        result = subprocess.run(["limactl", *args], capture_output=True, text=True,
                                timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ValueError("limactl not found; this script drives the Lima VMs from docs/usage.md") from error
    if result.returncode != 0:
        raise ValueError(f"limactl {' '.join(args)} failed: {result.stderr.strip()[-500:]}")
    return result.stdout


def in_vm(vm, script):
    """Run a shell script as root inside a Lima VM and return its output."""
    return limactl("shell", "--workdir", "/", vm, "sudo", "sh", "-c", script, timeout=60)


def vm_running(vm):
    return limactl("list", "--format", "{{.Status}}", vm, timeout=30).strip() == "Running"


def now():
    return time.strftime("%H:%M:%S")


def first_line(text, width=40):
    return (text.strip().splitlines() or [""])[0][:width]


def node_status(gateway, api_key, name):
    """The node's status in the scheduler (GET /nodes through the gateway)."""
    status, text = call("GET", f"{gateway}/nodes", api_key)
    if status != 200:
        return f"(GET /nodes: HTTP {status})"
    statuses = {node["id"]: node["status"] for node in json.loads(text)}
    return statuses.get(name, "missing")


def wait_status(gateway, api_key, name, want, timeout):
    start = time.monotonic()
    while node_status(gateway, api_key, name) != want:
        if time.monotonic() - start > timeout:
            raise ValueError(f"the scheduler did not show {name} as {want} within {timeout} s")
        time.sleep(0.5)
    return time.monotonic() - start


def sandboxes_on(name, api_key, run=None):
    """(sandbox ID, state) pairs on one node, asking the node directly; None if it does not answer."""
    query = [("state", "running"), ("state", "paused")]
    if run:
        query.append(("metadata", f"repro-run={run}"))
    status, text = call("GET", f"{NODES[name][1]}/v2/sandboxes?{urllib.parse.urlencode(query)}", api_key)
    if status != 200:
        return None
    return [(sandbox["sandboxID"], sandbox.get("state", "?")) for sandbox in json.loads(text)]


def holder(api_key, run, sid):
    for name in NODES:
        if any(found == sid for found, _ in sandboxes_on(name, api_key, run) or []):
            return name
    return "?"


def unhealthy_gauge():
    status, text = call("GET", SCHEDULER_METRICS)
    for line in text.splitlines() if status == 200 else []:
        if line.startswith(UNHEALTHY_GAUGE):
            return line.rsplit(" ", 1)[1]
    return "?"


def repository_backend(vm):
    return in_vm(vm, f"sed -n 's/^repository_backend *= *\"\\(.*\\)\"/\\1/p' {NODE_CONFIG}").strip() or "?"


def create_two_on_dead_node(gateway, api_key, body, run):
    """Create sandboxes through the gateway until two land on node-b; delete the others."""
    on_dead = []
    for attempt in range(1, 9):
        status, text = call("POST", f"{gateway}/sandboxes-cold", api_key, body=body, timeout=180)
        if status != 201:
            raise ValueError(f"create {attempt} returned HTTP {status}: {first_line(text, 200)}")
        sid = json.loads(text)["sandboxID"]
        name = holder(api_key, run, sid)
        if name not in NODES:
            raise ValueError(f"no node lists the new sandbox {sid}")
        if name == DEAD:
            on_dead.append(sid)
        else:
            call("DELETE", f"{NODES[name][1]}/sandboxes/{sid}", api_key, timeout=60)
        print(f"  create {attempt}: sandbox {sid} on {name}" + ("" if name == DEAD else ", deleted"))
        if len(on_dead) == 2:
            return on_dead
    raise ValueError(f"8 creates did not put 2 sandboxes on {DEAD}")


def wait_serving(url, timeout):
    start = time.monotonic()
    while call("GET", f"{url}/health", timeout=3)[0] // 100 != 2:
        if time.monotonic() - start > timeout:
            raise ValueError(f"{url} did not answer /health within {timeout} s")
        time.sleep(1)


def gateway_answer(gateway, api_key, sid):
    status, text = call("GET", f"{gateway}/sandboxes/{sid}", api_key)
    if status == 200:
        return status, f"state {json.loads(text).get('state', '?')}"
    return status, first_line(text, 32)


def cleanup(api_key, run, snapshot):
    for name, (_, url) in NODES.items():
        held = sandboxes_on(name, api_key, run)
        if held is None:
            print(f"[{now()}] cleanup: {name} does not answer; delete its repro-run={run} sandboxes by hand")
        for sid, _ in held or []:
            status = call("DELETE", f"{url}/sandboxes/{sid}", api_key, timeout=60)[0]
            print(f"[{now()}] cleanup: delete sandbox {sid} on {name}: HTTP {status}")
    if snapshot:
        for name, (_, url) in NODES.items():
            status = call("DELETE", f"{url}/snapshots/{snapshot}", api_key, timeout=60)[0]
            if status == 204:
                print(f"[{now()}] cleanup: delete snapshot {snapshot} on {name}: HTTP {status}")
                break
        else:
            print(f"[{now()}] cleanup: no node deleted snapshot {snapshot}; delete it by hand")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--image", default="ubuntu:22.04", help="OCI image for the sandboxes")
    parser.add_argument("--watch", type=int, default=120, help="seconds to watch after node-b dies")
    parser.add_argument("--every", type=int, default=30, help="seconds between checks while watching")
    args = parser.parse_args()
    gw = args.gateway
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    vm = NODES[DEAD][0]
    if call("GET", f"{gw}/nodes", api_key)[0] != 200:
        raise ValueError(f"the gateway at {gw} does not answer GET /nodes")
    for name in NODES:
        if node_status(gw, api_key, name) != "ready":
            raise ValueError(f"{name} must be ready first")
        if sandboxes_on(name, api_key):
            raise ValueError(f"{name} must hold no sandboxes first")

    run = uuid.uuid4().hex[:12]
    body = {"image": args.image, "cpuCount": 1, "memoryMB": 512, "timeout": 1800,
            "autoPause": False, "metadata": {"repro-run": run}}
    backends = {name: repository_backend(NODES[name][0]) for name in NODES}
    print(f"run {run}: gateway {gw}; node-a = VM agentenv {NODES['node-a'][1]},"
          f" node-b = VM agentenv-2 {NODES['node-b'][1]}")
    print("snapshot repository: " + ", ".join(f"{name} {backend}" for name, backend in backends.items())
          + f" (repository_backend in {NODE_CONFIG})")
    print(f"Each sandbox: POST /sandboxes-cold, {args.image}, 1 vCPU, 512 MiB, label repro-run={run}.")

    snapshot = None
    powered_off = False
    relaunched = []
    try:
        print(f"\n== Step 1: two sandboxes on {DEAD}: A keeps running and gets a snapshot, B is paused")
        a, b = create_two_on_dead_node(gw, api_key, body, run)
        status, text = call("POST", f"{gw}/sandboxes/{a}/snapshots", api_key, body={}, timeout=600)
        if status != 201:
            raise ValueError(f"snapshot of A returned HTTP {status}: {first_line(text, 200)}")
        snapshot = json.loads(text)["snapshotID"]
        print(f"[{now()}] A = {a}: snapshot S = {snapshot} (HTTP {status})")
        status, text = call("POST", f"{gw}/sandboxes/{b}/pause", api_key, timeout=120)
        if status != 204:
            raise ValueError(f"pause of B returned HTTP {status}: {first_line(text, 200)}")
        print(f"[{now()}] B = {b}: paused (HTTP {status})")
        seen = [name for name, (_, url) in NODES.items()
                if call("GET", f"{url}/snapshots/{snapshot}", api_key)[0] == 200]
        print(f"[{now()}] S is visible on: {', '.join(seen) or 'no node'} (GET /snapshots/S on each node)")
        status, text = call("GET", f"{NODES[DEAD][1]}/snapshots?sandboxID={b}", api_key)
        count = len(json.loads(text)) if status == 200 else "?"
        print(f"[{now()}] snapshots of B on {DEAD}: {count} (GET /snapshots?sandboxID=B; the pause made none)")
        for label, sid in (("A", a), ("B", b)):
            status, text = gateway_answer(gw, api_key, sid)
            print(f"[{now()}] GET /sandboxes/{label} through the gateway: HTTP {status}, {text}")

        print(f"\n== Step 2: {DEAD}'s machine dies")
        on_a_before = {sid for sid, _ in sandboxes_on("node-a", api_key) or []}
        log_size = os.path.getsize(SCHEDULER_LOG) if os.path.exists(SCHEDULER_LOG) else None
        limactl("stop", "--force", vm, timeout=120)
        powered_off = True
        died = time.monotonic()
        print(f"[{now()}] powered off VM {vm} (limactl stop --force): every process and VM on it is gone")

        print(f"\n== Step 3: watch for {args.watch} s")
        unhealthy_after = None
        for offset in range(0, args.watch + 1, args.every):
            while True:
                if unhealthy_after is None and node_status(gw, api_key, DEAD) == "unhealthy":
                    unhealthy_after = time.monotonic() - died
                left = died + offset - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(1.0, left))
            status_dead = node_status(gw, api_key, DEAD)
            answer_a = gateway_answer(gw, api_key, a)
            answer_b = gateway_answer(gw, api_key, b)
            on_a = sandboxes_on("node-a", api_key) or []
            new = [sid for sid, _ in on_a if sid not in on_a_before]
            relaunched.extend(new)
            relaunched.extend(f"{label} answered HTTP 200" for label, answer in (("A", answer_a), ("B", answer_b))
                              if answer[0] == 200)
            print(f"  +{offset:>3} s  {DEAD}: {status_dead:<9} | gateway: A {answer_a[0]} {answer_a[1]!r},"
                  f" B {answer_b[0]} | node-a holds {len(on_a)} sandbox(es)"
                  + (f", new: {', '.join(new)}" if new else "")
                  + f" | unhealthy-node gauge {unhealthy_gauge()}")
        status, text = call("POST", f"{gw}/sandboxes/{b}/resume", api_key, body={"timeout": 600})
        print(f"[{now()}] resume B through the gateway: HTTP {status} {first_line(text, 32)!r}")
        if log_size is not None:
            with open(SCHEDULER_LOG) as log:
                log.seek(log_size)
                written = [line for line in log.read().splitlines() if line.strip()]
            print(f"[{now()}] lines the scheduler wrote to {SCHEDULER_LOG} since {DEAD} died: {len(written)}")
            for line in written[:5]:
                print(f"    {line[:160]}")

        print("\n== Step 4: the agent relaunches A from S by hand")
        status, text = call("GET", f"{gw}/snapshots?sandboxID={a}", api_key)
        found = [item["snapshotID"] for item in json.loads(text)] if status == 200 else []
        print(f"[{now()}] GET /snapshots?sandboxID=A through the gateway: HTTP {status},"
              f" {len(found)} snapshot(s)" + (f": {', '.join(found)}" if found else ""))
        status, text = call("GET", f"{gw}/snapshots?sandboxID={b}", api_key)
        count = len(json.loads(text)) if status == 200 else "?"
        print(f"[{now()}] GET /snapshots?sandboxID=B through the gateway: HTTP {status}, {count} snapshot(s)")
        launch = {"templateID": snapshot, "timeout": 1800, "autoPause": False, "metadata": {"repro-run": run}}
        status, text = call("POST", f"{gw}/sandboxes", api_key, body=launch, timeout=600)
        if status == 201:
            new_id = json.loads(text)["sandboxID"]
            print(f"[{now()}] POST /sandboxes with templateID S: HTTP 201, new sandbox {new_id}"
                  f" on {holder(api_key, run, new_id)} (A was {a})")
        else:
            print(f"[{now()}] POST /sandboxes with templateID S: HTTP {status} {first_line(text, 100)!r}")
        manual = status == 201

        print(f"\n== Step 5: {DEAD}'s machine comes back")
        limactl("start", "--tty=false", vm, timeout=600)
        wait_serving(NODES[DEAD][1], 300)
        powered_off = False
        waited = wait_status(gw, api_key, DEAD, "ready", 120)
        print(f"[{now()}] started VM {vm}; {DEAD} serves again and the scheduler says ready ({waited:.0f} s"
              f" after it answered; {time.monotonic() - died:.0f} s after it died)")
        for label, sid in (("A", a), ("B", b)):
            status, text = gateway_answer(gw, api_key, sid)
            print(f"[{now()}] GET /sandboxes/{label} through the gateway: HTTP {status}, {text}")
    finally:
        print()
        if powered_off:
            if not vm_running(vm):
                print(f"[{now()}] starting VM {vm} again")
                limactl("start", "--tty=false", vm, timeout=600)
            wait_serving(NODES[DEAD][1], 300)
        cleanup(api_key, run, snapshot)

    print("\n== Result")
    if relaunched:
        print(f"FIXED: something relaunched A or B within {args.watch} s:"
              f" {', '.join(dict.fromkeys(relaunched))}.")
        return 0
    unhealthy = f"{unhealthy_after:.0f} s" if unhealthy_after is not None else "never"
    print(f"REPRODUCED: for {args.watch} s after {DEAD} died, nothing relaunched A or B, and the gateway"
          f" answered every request for them with an error. The only signs were {DEAD}'s status"
          f" (unhealthy after {unhealthy}) and the scheduler's unhealthy-node gauge.")
    print("The agent's own relaunch from S " + ("worked: a new sandbox with a new ID." if manual else
          "failed: node-a could not find S."))
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
