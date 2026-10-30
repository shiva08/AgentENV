#!/usr/bin/env python3
"""Show that the scheduler still sends creates to a node it marks unhealthy.

Uses the two-node Lima setup from docs/usage.md: the gateway on
127.0.0.1:8090, node-a (VM agentenv) on :8000 and node-b (VM agentenv-2) on
:8001. Nodes send the scheduler a heartbeat every 5 s, and the scheduler marks
a node unhealthy once its last heartbeat is more than report_ttl (30 s) old.

  Step 1  Both nodes healthy: send a few creates, show which node got each.
  Step 2  node-b stops sending heartbeats. Wait until the scheduler marks it
          unhealthy, then send the same creates.
            --mode crash      kill node-b's server (no heartbeats, no service)
            --mode silence    block only node-b's heartbeats (server keeps running)
            --mode crash-now  kill node-b's server, but send the creates right
                              away, while the scheduler still lists it as ready
  Step 3  Make node-b healthy again and send the same creates.

Each create is POST /sandboxes-cold with body {}. A node that receives it
rejects it with 422, so no sandbox is created. Each node's own request counter
(GET /metrics) shows which node received each create.

Example:
  python3 docs/proofs/05-failure-detection/scripts/reproduce-unhealthy-node-scheduling.py --mode crash

Exit status: 1 if a create in step 2 went to node-b (bug reproduced), 0 if
not, 2 on setup errors. node-b is restored on exit, including after Ctrl-C, so
the script can be rerun right away.
"""

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse

# Node ID -> (Lima VM, node API URL as listed in services/config/lima.json).
NODES = {
    "node-a": ("agentenv", "http://127.0.0.1:8000"),
    "node-b": ("agentenv-2", "http://127.0.0.1:8001"),
}
TARGET = "node-b"
SCHEDULER_PORT = 9090
BLOCK_TABLE = "repro_block_heartbeats"


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


def scheduler_view(gateway, api_key):
    status, text = call("GET", f"{gateway}/nodes", api_key)
    if status != 200:
        raise ValueError(f"GET {gateway}/nodes returned HTTP {status}: {text[:200]}")
    listed = {node["id"]: node["status"] for node in json.loads(text)}
    return {node: listed.get(node, "absent") for node in NODES}


def show(view):
    return " ".join(f"{node}={state}" for node, state in view.items())


def wait_for(gateway, api_key, node, state, timeout):
    start = time.monotonic()
    while True:
        view = scheduler_view(gateway, api_key)
        if view[node] == state:
            return view
        if time.monotonic() - start > timeout:
            raise ValueError(f"{node} was not {state} after {timeout} s; scheduler says: {show(view)}")
        time.sleep(1)


def creates_received(node_url):
    """Count POST /sandboxes-cold requests the node has answered; None if it is down."""
    status, text = call("GET", f"{node_url}/metrics", timeout=5)
    if status != 200:
        return None
    total = 0
    for line in text.splitlines():
        if (line.startswith("agentenv_http_request_duration_seconds_count{")
                and 'method="POST"' in line and 'route="/sandboxes-cold"' in line):
            total += int(float(line.rsplit(" ", 1)[1]))
    return total


def send_creates(gateway, api_key, count):
    """Send creates through the gateway and return the node each one went to."""
    went_to = []
    for i in range(1, count + 1):
        before = {node: creates_received(url) for node, (_, url) in NODES.items()}
        status, text = call("POST", f"{gateway}/sandboxes-cold", api_key, body={}, timeout=60)
        after = {node: creates_received(url) for node, (_, url) in NODES.items()}
        reached = [node for node in NODES
                   if None not in (before[node], after[node]) and after[node] > before[node]]
        down = [node for node in NODES if after[node] is None]
        if len(reached) == 1:
            node, where = reached[0], f"reached {reached[0]}"
        elif not reached and status == 502 and len(down) == 1:
            node, where = down[0], f"reached no running node, so it went to {down[0]} (down)"
        else:
            node, where = None, "could not tell which node got it"
        reply = (text.strip().splitlines() or [""])[0][:32]
        print(f"  create {i}: HTTP {status} {reply!r:36} -> {where}")
        went_to.append(node)
    return went_to


def tally(went_to):
    return ", ".join(f"{node} got {went_to.count(node)}" for node in NODES)


def stop_heartbeats(mode):
    vm = NODES[TARGET][0]
    if mode in ("crash", "crash-now"):
        # SIGKILL skips the clean shutdown, so the node cannot unregister.
        in_vm(vm, "systemctl kill -s SIGKILL aenv; systemctl stop aenv")
        return f"killed {TARGET}'s server (SIGKILL): it no longer serves or sends heartbeats"
    in_vm(vm, f"nft add table inet {BLOCK_TABLE}"
              f" && nft add chain inet {BLOCK_TABLE} out '{{ type filter hook output priority 0; }}'"
              f" && nft add rule inet {BLOCK_TABLE} out tcp dport {SCHEDULER_PORT} reject with tcp reset")
    return (f"blocked {TARGET}'s traffic to the scheduler (port {SCHEDULER_PORT}): "
            f"it keeps serving, but its heartbeats fail")


def restore():
    in_vm(NODES[TARGET][0], f"nft delete table inet {BLOCK_TABLE} 2>/dev/null; systemctl start aenv")


def wait_until_refused(node_url, timeout):
    """Wait until the node's port refuses connections; Lima's forward accepts and drops them for a moment first."""
    parts = urllib.parse.urlsplit(node_url)
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            socket.create_connection((parts.hostname, parts.port), timeout=1).close()
        except ConnectionRefusedError:
            return
        except OSError:
            pass
        time.sleep(0.2)
    raise ValueError(f"{node_url} still accepted connections after {timeout} s")


def wait_until_serving(node_url, timeout):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if call("GET", f"{node_url}/health", timeout=2)[0] // 100 == 2:
            return
        time.sleep(0.5)
    raise ValueError(f"{node_url} did not answer /health after {timeout} s")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--mode", choices=["crash", "silence", "crash-now"], default="crash",
                        help="how node-b stops sending heartbeats (default: crash)")
    parser.add_argument("--requests", type=int, default=4, help="creates per step (at least 2)")
    args = parser.parse_args()
    if args.requests < 2:
        raise ValueError("--requests must be at least 2, so round-robin reaches both nodes")
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    gw = args.gateway

    print(f"gateway {gw}; " + "; ".join(f"{node} = VM {vm}, {url}" for node, (vm, url) in NODES.items()))
    print("Each create is POST /sandboxes-cold with body {}: the node that gets it answers 422,")
    print("so nothing is created. Each node's request counter (GET /metrics) shows who got it.")
    print(f"\n[{now()}] preparing: make sure {TARGET} is running and its heartbeats are not blocked")
    restore()
    try:
        print("\n== Step 1: both nodes healthy")
        wait_for(gw, api_key, "node-a", "ready", 60)
        view = wait_for(gw, api_key, TARGET, "ready", 120)
        print(f"[{now()}] scheduler says: {show(view)}")
        print(f"  => {tally(send_creates(gw, api_key, args.requests))}")

        print(f"\n== Step 2: {TARGET} stops sending heartbeats (--mode {args.mode})")
        stopped = time.monotonic()
        print(f"[{now()}] {stop_heartbeats(args.mode)}")
        if args.mode == "crash-now":
            print(f"[{now()}] waiting until {TARGET}'s port refuses connections ...")
            wait_until_refused(NODES[TARGET][1], 15)
            view = scheduler_view(gw, api_key)
            if view[TARGET] != "ready":
                raise ValueError(f"{TARGET} is no longer ready; scheduler says: {show(view)}")
            print(f"[{now()}] {TARGET}'s port refuses connections, but scheduler still says: {show(view)}   "
                  f"({time.monotonic() - stopped:.0f} s after the kill)")
        else:
            print(f"[{now()}] waiting until the scheduler marks {TARGET} unhealthy (no heartbeat for 30 s) ...")
            view = wait_for(gw, api_key, TARGET, "unhealthy", 90)
            print(f"[{now()}] scheduler says: {show(view)}   "
                  f"({time.monotonic() - stopped:.0f} s after {TARGET} went silent)")
        went_to = send_creates(gw, api_key, args.requests)
        print(f"[{now()}] scheduler still says: {show(scheduler_view(gw, api_key))}")
        bad = went_to.count(TARGET)
        print(f"  => {tally(went_to)}")
        if bad and args.mode in ("crash", "crash-now"):
            print(f"     The {bad} create(s) sent to {TARGET} failed with HTTP 502 and created nothing.")
            print("     The gateway did not try node-a instead: node-a's counter did not move for them.")
        elif bad:
            print(f"     {TARGET} is still running, so it answered the {bad} create(s) sent to it.")

        print(f"\n== Step 3: {TARGET} healthy again")
        restored = time.monotonic()
        restore()
        if args.mode in ("crash", "crash-now"):
            print(f"[{now()}] started {TARGET}'s server")
        else:
            print(f"[{now()}] removed the block; {TARGET} retries heartbeats with backoff (up to 60 s)")
        wait_for(gw, api_key, TARGET, "ready", 120)
        wait_until_serving(NODES[TARGET][1], 120)
        view = scheduler_view(gw, api_key)
        print(f"[{now()}] scheduler says: {show(view)}   ({time.monotonic() - restored:.0f} s later)")
        print(f"  => {tally(send_creates(gw, api_key, args.requests))}")
    finally:
        restore()

    print("\n== Result")
    if args.mode == "crash-now":
        if bad:
            print(f"BUG REPRODUCED: {bad} of {args.requests} creates went to {TARGET} after it crashed, "
                  f"while the scheduler still listed it as ready.")
            return 1
        print(f"PASS: every create reached a running node, although the scheduler still listed {TARGET} as ready.")
        return 0
    if bad:
        print(f"BUG REPRODUCED: the scheduler sent {bad} of {args.requests} creates to {TARGET} "
              f"while it marked {TARGET} unhealthy.")
        return 1
    print(f"PASS: the scheduler sent no creates to {TARGET} while it was unhealthy.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
