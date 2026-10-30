#!/usr/bin/env python3
"""Show that the scheduler puts more sandboxes on a node than its limit allows.

Uses the two-node Lima setup from docs/usage.md: the gateway on 127.0.0.1:8090,
node-a (VM agentenv) on :8000, node-b (VM agentenv-2) on :8001 and the
scheduler on 127.0.0.1:9090. The script restarts the scheduler with
services/config/lima.json plus a limit of one sandbox per node
(node_resource_limit.max_sandbox_count = 1), then:

  1. Both nodes empty: send 4 creates at once. At most 1 may land on each node.
  2. Each node holds 1 sandbox, and the scheduler has seen it in a heartbeat:
     send 1 more create. It must be refused, not placed.
  3. Both nodes empty: send a create the node rejects (body {}, HTTP 422), then
     2 creates at once. Both must be placed: the rejected create must not keep
     a slot.

Each sandbox is ubuntu:22.04 with 1 vCPU and 512 MiB, labelled
repro-run=<run>, and is deleted afterwards. Before steps 2 and 3 the script
waits until the scheduler (GET /nodes) shows both nodes empty again. At the
end it restarts the scheduler with services/config/lima.json alone, detached,
logging to /tmp/verify-bindings-scheduler.log.

Examples:
  python3 docs/proofs/07-admission-control/scripts/reproduce-capacity-overfill.py
  python3 docs/proofs/07-admission-control/scripts/reproduce-capacity-overfill.py --scheduler-bin /tmp/scheduler-pre-capacity

Exit status: 0 if no node got more than one sandbox and no slot was kept,
1 if a node got more or a slot was kept, 2 on setup errors.
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

# This file is in docs/proofs/<point>/scripts/.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
# Node ID -> (Lima VM, node API URL as listed in services/config/lima.json).
NODES = {
    "node-a": ("agentenv", "http://127.0.0.1:8000"),
    "node-b": ("agentenv-2", "http://127.0.0.1:8001"),
}
CONFIG = "services/config/lima.json"
LIMITED_CONFIG = "/tmp/reproduce-capacity-scheduler.json"
LIMIT = 1
SCHEDULER_PORT = 9090
SCHEDULER_PATTERN = r"scheduler[^ ]* -config"
SCHEDULER_LOG = "/tmp/verify-bindings-scheduler.log"


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


def in_vm(vm, script):
    """Run a shell script as root inside a Lima VM and return its output."""
    try:
        result = subprocess.run(
            ["limactl", "shell", "--workdir", "/", vm, "sudo", "sh", "-c", script],
            capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise ValueError("limactl not found; this script drives the Lima VMs from docs/usage.md") from error
    if result.returncode != 0:
        raise ValueError(f"{vm}: `{script}` failed: {result.stderr.strip()}")
    return result.stdout


def now():
    return time.strftime("%H:%M:%S")


def first_line(text, width=40):
    return (text.strip().splitlines() or [""])[0][:width]


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False


def stop_scheduler():
    subprocess.run(["pkill", "-f", SCHEDULER_PATTERN], check=False)
    start = time.monotonic()
    while subprocess.run(["pgrep", "-f", SCHEDULER_PATTERN], capture_output=True).returncode == 0:
        if time.monotonic() - start > 20:
            raise ValueError("the scheduler was still running 20 s after pkill")
        time.sleep(0.2)


def restart_scheduler(binary, config):
    stop_scheduler()
    marker = f"=== {time.time():.3f} started by {os.path.basename(__file__)}"
    with open(SCHEDULER_LOG, "a") as log:
        log.write(f"\n{marker}\n")
        log.flush()
        proc = subprocess.Popen([binary, "-config", config], cwd=REPO, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    start = time.monotonic()
    while True:
        with open(SCHEDULER_LOG) as log:
            listening = "gRPC server listening" in log.read().rsplit(marker, 1)[-1]
        if listening and port_open(SCHEDULER_PORT):
            return f"pid {proc.pid}"
        if proc.poll() is not None or time.monotonic() - start > 15:
            raise ValueError(f"the scheduler did not start; see {SCHEDULER_LOG}")
        time.sleep(0.2)


def scheduler_view(gateway, api_key):
    """Each node's entry in GET /nodes, or None while the gateway cannot answer."""
    status, text = call("GET", f"{gateway}/nodes", api_key)
    if status != 200:
        return None
    view = {node["id"]: node for node in json.loads(text)}
    return view if all(name in view for name in NODES) else None


def wait_view(gateway, api_key, want, what, timeout=90):
    """Wait until the scheduler's view satisfies want; return (view, seconds waited)."""
    start = time.monotonic()
    while True:
        view = scheduler_view(gateway, api_key)
        if view and want(view):
            return view, time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise ValueError(f"the scheduler did not show {what} within {timeout} s")
        time.sleep(0.5)


def ready(view):
    return all(view[name]["status"] == "ready" for name in NODES)


def empty(view):
    return ready(view) and all(
        view[name].get("sandboxCount", 0) + view[name].get("sandboxStartingCount", 0) == 0 for name in NODES)


def describe(view):
    return ", ".join(f"{name} {view[name].get('sandboxCount', 0)} running"
                     f" + {view[name].get('sandboxStartingCount', 0)} starting" for name in NODES)


def create(gateway, api_key, body):
    status, text = call("POST", f"{gateway}/sandboxes-cold", api_key, body=body, timeout=180)
    sid = json.loads(text).get("sandboxID") if status // 100 == 2 else None
    return status, sid, first_line(text)


def create_at_once(gateway, api_key, body, count):
    results = [None] * count
    barrier = threading.Barrier(count)

    def worker(index):
        barrier.wait()
        results[index] = create(gateway, api_key, body)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def holders(api_key, run):
    """Sandbox ID -> node, for this run's sandboxes, asking each node directly."""
    query = urllib.parse.urlencode({"metadata": f"repro-run={run}"})
    held = {}
    for name, (_, url) in NODES.items():
        status, text = call("GET", f"{url}/v2/sandboxes?{query}", api_key)
        if status != 200:
            raise ValueError(f"listing sandboxes on {name} returned HTTP {status}: {first_line(text, 200)}")
        for sandbox in json.loads(text):
            held[sandbox["sandboxID"]] = name
    return held


def report(results, held):
    for index, (status, sid, text) in enumerate(results, 1):
        if sid:
            print(f"  create {index}: HTTP {status}, sandbox {sid} on {held.get(sid, '?')}")
        else:
            print(f"  create {index}: HTTP {status} {text!r}")


def per_node(held):
    return {name: sum(1 for holder in held.values() if holder == name) for name in NODES}


def show_per_node(counts):
    print("  => " + ", ".join(f"{name} holds {count}" for name, count in counts.items())
          + f" (limit {LIMIT} per node)")


def over_limit(counts):
    return [f"{name} {count}/{LIMIT}" for name, count in counts.items() if count > LIMIT]


def delete_all(api_key, run):
    held = holders(api_key, run)
    for sid, name in held.items():
        status = call("DELETE", f"{NODES[name][1]}/sandboxes/{sid}", api_key, timeout=60)[0]
        if status != 204:
            print(f"  cleanup: deleting {sid} on {name} returned HTTP {status}")
    return len(held)


def clear(gateway, api_key, run):
    deleted = delete_all(api_key, run)
    print(f"[{now()}] deleted {deleted} sandbox(es) directly on the nodes")
    _, waited = wait_view(gateway, api_key, empty, "both nodes empty")
    print(f"[{now()}] scheduler says both nodes are empty ({waited:.1f} s later)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--scheduler-bin", default="services/bin/scheduler", help="scheduler binary")
    parser.add_argument("--image", default="ubuntu:22.04", help="OCI image for the sandboxes")
    args = parser.parse_args()
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    gw = args.gateway
    binary = os.path.join(REPO, args.scheduler_bin)
    if not os.path.exists(binary):
        raise ValueError(f"{binary} not found; build it with make -C services build")
    if call("GET", f"{gw}/nodes", api_key)[0] == 0:
        raise ValueError(f"the gateway at {gw} does not answer")

    with open(os.path.join(REPO, CONFIG)) as source:
        config = json.load(source)
    config["scheduler"]["node_resource_limit"] = {"max_sandbox_count": LIMIT}
    with open(LIMITED_CONFIG, "w") as target:
        json.dump(config, target, indent=2)

    run = uuid.uuid4().hex[:12]
    body = {"image": args.image, "cpuCount": 1, "memoryMB": 512, "timeout": 600,
            "autoPause": False, "metadata": {"repro-run": run}}
    print(f"run {run}: scheduler {args.scheduler_bin}, gateway {gw}; node-a = VM agentenv"
          f" {NODES['node-a'][1]}, node-b = VM agentenv-2 {NODES['node-b'][1]}")
    print(f"Each create: POST /sandboxes-cold, {args.image}, 1 vCPU, 512 MiB, label repro-run={run}.")

    problems = []
    try:
        print(f"[{now()}] restarted the scheduler with node_resource_limit.max_sandbox_count = {LIMIT}:"
              f" {restart_scheduler(binary, LIMITED_CONFIG)}")
        view, _ = wait_view(gw, api_key, ready, "both nodes ready")
        if not empty(view) or any(view[name].get("sandboxPausedCount", 0) for name in NODES):
            raise ValueError(f"both nodes must be empty first; scheduler says: {describe(view)}")
        print(f"[{now()}] scheduler says: {describe(view)}")

        print(f"\n== Step 1: both nodes empty, 4 creates at once")
        results = create_at_once(gw, api_key, body, 4)
        held = holders(api_key, run)
        report(results, held)
        counts = per_node(held)
        show_per_node(counts)
        if over_limit(counts):
            problems.append(f"4 creates at once left {', '.join(over_limit(counts))}")
        clear(gw, api_key, run)

        print(f"\n== Step 2: each node holds 1 sandbox, then 1 more create")
        for index in (1, 2):
            status, sid, text = create(gw, api_key, body)
            location = holders(api_key, run).get(sid, "?") if sid else ""
            print(f"  create {index}: HTTP {status}" + (f", sandbox {sid} on {location}" if sid else f" {text!r}"))
        view, waited = wait_view(gw, api_key, lambda v: ready(v) and all(
            v[name].get("sandboxCount", 0) >= 1 for name in NODES), "1 running sandbox on each node")
        print(f"[{now()}] scheduler says: {describe(view)} ({waited:.1f} s later)")
        status, sid, text = create(gw, api_key, body)
        held = holders(api_key, run)
        print(f"  create 3: HTTP {status}" + (f", sandbox {sid} on {held.get(sid, '?')}" if sid else f" {text!r}"))
        counts = per_node(held)
        show_per_node(counts)
        if over_limit(counts):
            problems.append(f"1 create on full nodes left {', '.join(over_limit(counts))}")
        clear(gw, api_key, run)

        print(f"\n== Step 3: a create the node rejects, then 2 creates at once")
        status, _, text = create(gw, api_key, {})
        print(f"  create with body {{}}: HTTP {status} {text!r}")
        results = create_at_once(gw, api_key, body, 2)
        held = holders(api_key, run)
        report(results, held)
        counts = per_node(held)
        show_per_node(counts)
        if over_limit(counts):
            problems.append(f"2 creates at once left {', '.join(over_limit(counts))}")
        if any(status == 503 for status, _, _ in results):
            problems.append("a create was refused although a slot should be free after the rejected create")
    finally:
        print()
        try:
            deleted = delete_all(api_key, run)
            print(f"[{now()}] cleanup: deleted {deleted} sandbox(es)")
        except ValueError as error:
            print(f"[{now()}] cleanup failed: {error}")
        print(f"[{now()}] restarted the scheduler with {CONFIG} alone:"
              f" {restart_scheduler(binary, CONFIG)}, log {SCHEDULER_LOG}")

    print("\n== Result")
    if problems:
        print("BUG REPRODUCED: " + "; ".join(problems) + ".")
        return 1
    print(f"PASS: no node got more than {LIMIT} sandbox, and the rejected create kept no slot.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
