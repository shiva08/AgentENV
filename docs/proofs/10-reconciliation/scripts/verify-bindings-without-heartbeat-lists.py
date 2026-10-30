#!/usr/bin/env python3
"""Show that the scheduler needs no sandbox lists from heartbeats when node names are fixed.

Uses the two-node Lima setup from docs/usage.md: the gateway on
127.0.0.1:8090, node-a (VM agentenv) on :8000, node-b (VM agentenv-2) on
:8001, and the scheduler on 127.0.0.1:9090, run from this repository. Node
names are fixed in services/config/lima.json, so the node the gateway records
for a sandbox at create never changes.

The same test runs with two scheduler settings:

  today    records expire 30 s after their last write and live in the
           scheduler's memory (services/config/lima.json as it is)
  durable  records do not expire in practice (binding_ttl 24h) and live in
           Redis (SCHEDULER_BINDING_TTL=24h SCHEDULER_REDIS_ADDR=127.0.0.1:6379)

The test:
  1. Create a sandbox through the gateway, which records the sandbox's node.
  2. Block heartbeats from both nodes, so no sandbox list reaches the scheduler.
  3. Look the sandbox up through the gateway right away, 35 s later (past the
     30 s expiry), and after restarting the scheduler.
  4. Delete the sandbox, look it up once more, and remove the blocks.

The script stops and starts the scheduler itself
(services/bin/scheduler -config services/config/lima.json). On exit it removes
the heartbeat blocks and leaves the scheduler running with today's settings,
detached, logging to /tmp/verify-bindings-scheduler.log.

Example:
  python3 docs/proofs/10-reconciliation/scripts/verify-bindings-without-heartbeat-lists.py

Exit status: 0 if every lookup worked with durable settings while today's
settings lost the record, 1 otherwise, 2 on setup errors.
"""

import argparse
import http.client
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse

# This file is in docs/proofs/<point>/scripts/.
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
# Node ID -> (Lima VM, node API URL as listed in services/config/lima.json).
NODES = {
    "node-a": ("agentenv", "http://127.0.0.1:8000"),
    "node-b": ("agentenv-2", "http://127.0.0.1:8001"),
}
SCHEDULER_PORT = 9090
SCHEDULER_CMD = ["services/bin/scheduler", "-config", "services/config/lima.json"]
SCHEDULER_LOG = "/tmp/verify-bindings-scheduler.log"
SETTINGS = {
    "today": {},
    "durable": {"SCHEDULER_BINDING_TTL": "24h", "SCHEDULER_REDIS_ADDR": "127.0.0.1:6379"},
}
DESCRIPTION = {
    "today": "records expire after 30 s and live in the scheduler's memory",
    "durable": "records do not expire (binding_ttl 24h) and live in Redis",
}
REDIS_KEY_PREFIX = "agentenv:scheduler:bindings:sandbox:"
BLOCK_TABLE = "repro_block_heartbeats"
SANDBOX = {"image": "ubuntu:22.04", "cpuCount": 1, "memoryMB": 512, "timeout": 300,
           "autoPause": False, "metadata": {"repro": "bindings-without-lists"}}
CHECKS = ["right after the block", "35 s later", "after a scheduler restart"]


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
    try:
        conn.request(method, parts.path, body=data, headers=headers)
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
    # Wait for the process to exit: it closes the gRPC port before its metrics port.
    pattern = "bin/scheduler -config"
    subprocess.run(["pkill", "-f", pattern], check=False)
    start = time.monotonic()
    while subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0:
        if time.monotonic() - start > 20:
            raise ValueError("the scheduler was still running 20 s after pkill")
        time.sleep(0.2)


def start_scheduler(settings):
    env = {key: value for key, value in os.environ.items() if key not in SETTINGS["durable"]}
    env.update(SETTINGS[settings])
    marker = f"=== {time.time():.3f} started by {os.path.basename(__file__)} with {settings} settings"
    with open(SCHEDULER_LOG, "a") as log:
        log.write(f"\n{marker}\n")
        log.flush()
        proc = subprocess.Popen(SCHEDULER_CMD, cwd=REPO, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    start = time.monotonic()
    while True:
        with open(SCHEDULER_LOG) as log:
            found = re.search(r'"binding_store":\s*"(\w+)"', log.read().rsplit(marker, 1)[-1])
        if found and port_open(SCHEDULER_PORT):
            return proc, found.group(1)
        if proc.poll() is not None or time.monotonic() - start > 15:
            raise ValueError(f"the scheduler did not start; see {SCHEDULER_LOG}")
        time.sleep(0.2)


def restart_scheduler(settings):
    stop_scheduler()
    proc, store = start_scheduler(settings)
    ttl = SETTINGS[settings].get("SCHEDULER_BINDING_TTL", "30s")
    return f"pid {proc.pid}, binding store {store}, binding_ttl {ttl}"


def wait_ready(gateway, api_key, timeout=120):
    start = time.monotonic()
    while True:
        status, text = call("GET", f"{gateway}/nodes", api_key)
        if status == 200:
            listed = {node["id"]: node["status"] for node in json.loads(text)}
            if all(listed.get(node) == "ready" for node in NODES):
                return
        if time.monotonic() - start > timeout:
            raise ValueError(f"both nodes were not ready within {timeout} s (GET {gateway}/nodes)")
        time.sleep(1)


def block_heartbeats():
    for vm, _ in NODES.values():
        in_vm(vm, f"nft delete table inet {BLOCK_TABLE} 2>/dev/null;"
                  f" nft add table inet {BLOCK_TABLE}"
                  f" && nft add chain inet {BLOCK_TABLE} out '{{ type filter hook output priority 0; }}'"
                  f" && nft add rule inet {BLOCK_TABLE} out tcp dport {SCHEDULER_PORT} reject with tcp reset")


def unblock_heartbeats():
    for vm, _ in NODES.values():
        in_vm(vm, f"nft delete table inet {BLOCK_TABLE} 2>/dev/null; true")


def redis_record(sid):
    key = REDIS_KEY_PREFIX + sid
    try:
        value, ttl = (subprocess.run(["redis-cli", command, key], capture_output=True, text=True).stdout.strip()
                      for command in ("GET", "PTTL"))
    except FileNotFoundError:
        return "(redis-cli not found)"
    hours = f"{int(ttl) / 3_600_000:.1f} h" if ttl.isdigit() else f"PTTL {ttl}"
    return f"{key} = {value}, expires in {hours}"


def lookup(gateway, api_key, sid, timeout=15):
    """GET the sandbox through the gateway, waiting out its reconnect after a scheduler restart."""
    start = time.monotonic()
    while True:
        status, text = call("GET", f"{gateway}/sandboxes/{sid}", api_key)
        if status in (200, 404) or time.monotonic() - start > timeout:
            return status, first_line(text)
        time.sleep(0.2)


def run_test(settings, gateway, api_key, created):
    print(f"\n== Scheduler with {settings} settings: {DESCRIPTION[settings]}")
    print(f"[{now()}] restarted the scheduler: {restart_scheduler(settings)}")
    wait_ready(gateway, api_key)
    status, text = call("POST", f"{gateway}/sandboxes-cold", api_key, body=SANDBOX, timeout=180)
    if status // 100 != 2:
        raise ValueError(f"create through the gateway returned HTTP {status}: {first_line(text, 200)}")
    sid = json.loads(text)["sandboxID"]
    node = next((name for name, (_, url) in NODES.items()
                 if call("GET", f"{url}/sandboxes/{sid}", api_key)[0] == 200), None)
    if node is None:
        raise ValueError(f"no node has sandbox {sid}")
    created[sid] = node
    url = NODES[node][1]
    print(f"[{now()}] created sandbox {sid} through the gateway: HTTP {status}, on {node}")
    if settings == "durable":
        print(f"[{now()}] Redis: {redis_record(sid)}")

    results = {}
    try:
        block_heartbeats()
        blocked = time.monotonic()
        print(f"[{now()}] blocked heartbeats from both nodes: no sandbox list reaches the scheduler")
        for check in CHECKS:
            if check == "35 s later":
                time.sleep(max(0.0, 35 - (time.monotonic() - blocked)))
            elif check == "after a scheduler restart":
                print(f"[{now()}] restarted the scheduler: {restart_scheduler(settings)}")
            status, reply = lookup(gateway, api_key, sid)
            results[check] = status
            print(f"[{now()}] GET through the gateway, {check}: HTTP {status} {reply!r}")
        print(f"[{now()}] GET directly on {node}: HTTP {call('GET', f'{url}/sandboxes/{sid}', api_key)[0]}")
        if results[CHECKS[-1]] == 200:
            deleted = call("DELETE", f"{gateway}/sandboxes/{sid}", api_key, timeout=60)[0]
            print(f"[{now()}] DELETE through the gateway: HTTP {deleted}")
            status, reply = lookup(gateway, api_key, sid)
            who = ("the scheduler: no record" if "assignment" in reply
                   else f"{node}, because the record still points there")
            print(f"[{now()}] GET through the gateway after the delete: HTTP {status} {reply!r}  (answered by {who})")
        else:
            deleted = call("DELETE", f"{url}/sandboxes/{sid}", api_key, timeout=60)[0]
            print(f"[{now()}] DELETE directly on {node}, since the gateway lost the record: HTTP {deleted}")
        if deleted // 100 == 2:
            created.pop(sid)
    finally:
        unblock_heartbeats()
        print(f"[{now()}] removed the heartbeat blocks")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    args = parser.parse_args()
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    gw = args.gateway
    if not os.path.exists(os.path.join(REPO, SCHEDULER_CMD[0])):
        raise ValueError(f"{SCHEDULER_CMD[0]} not found; build it with make -C services build")
    if not port_open(6379):
        raise ValueError("Redis is not listening on 127.0.0.1:6379")
    if call("GET", f"{gw}/nodes", api_key)[0] == 0:
        raise ValueError(f"the gateway at {gw} does not answer")

    print(f"gateway {gw}; " + "; ".join(f"{node} = VM {vm}, {url}" for node, (vm, url) in NODES.items()))
    print("Node names are fixed in services/config/lima.json, so a sandbox's node never changes after create.")
    print(f"Scheduler output goes to {SCHEDULER_LOG}.")
    results, created = {}, {}
    unblock_heartbeats()
    try:
        for settings in SETTINGS:
            results[settings] = run_test(settings, gw, api_key, created)
    finally:
        for sid, node in created.items():
            call("DELETE", f"{NODES[node][1]}/sandboxes/{sid}", api_key, timeout=60)
        unblock_heartbeats()
        stop_scheduler()
        proc, _ = start_scheduler("today")
        print(f"\n[{now()}] left the scheduler running with today's settings (pid {proc.pid}, log {SCHEDULER_LOG})")

    print("\n== Result")
    print(f"  {'GET through the gateway':28} {'today':>7} {'durable':>8}")
    for check in CHECKS:
        print(f"  {check:28} {results['today'][check]:>7} {results['durable'][check]:>8}")
    lost = any(results["today"][check] == 404 for check in CHECKS[1:])
    kept = all(results["durable"][check] == 200 for check in CHECKS)
    if lost and kept:
        print("VERIFIED: with fixed node names and no heartbeat sandbox lists, the gateway kept finding the")
        print("sandbox once records did not expire and survived a scheduler restart. With today's settings")
        print("it lost the record, so today the list is needed only to refresh and rebuild records.")
        return 0
    print("NOT VERIFIED: see the table above.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
