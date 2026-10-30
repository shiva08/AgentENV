#!/usr/bin/env python3
"""Show that one node being down makes the list of all sandboxes fail.

Uses the two-node Lima setup from docs/usage.md: the gateway on 127.0.0.1:8090,
node-a (VM agentenv) on :8000 and node-b (VM agentenv-2) on :8001. To list
sandboxes (GET /sandboxes, GET /v2/sandboxes), the gateway asks every node and
merges the answers.

  Setup   Create sandboxes until each node holds one, labelled repro-run=<run>.
  Step 1  Both nodes up: list this run's sandboxes through the gateway.
  Step 2  Stop node-b's server (systemctl stop aenv; it pauses its sandbox to
          its own disk first), then list again. node-a's sandbox must still be
          listed, and the reply must name node-b as missing.
  Step 3  Start node-b again, wait until it is ready, and list again: all of
          this run's sandboxes must be back, and no node named as missing.

GET /sandboxes lists running sandboxes only, so after step 2 it no longer shows
node-b's (paused) sandbox; GET /v2/sandboxes asks for running and paused.
Each sandbox is ubuntu:22.04 with 1 vCPU and 512 MiB. All are deleted at the
end, and node-b is started again on exit, including after Ctrl-C.

Example:
  python3 docs/proofs/11-partial-results/scripts/reproduce-dead-node-breaks-list.py

Exit status: 1 if a list failed or was wrong while node-b was down or after it
came back (bug reproduced), 0 if not, 2 on setup errors.
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
TARGET = "node-b"
MISSING_HEADER = "x-agentenv-unavailable-nodes"


def call(method, url, api_key=None, body=None, timeout=15):
    """Return (HTTP status, body text, headers); status 0 means no HTTP response."""
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
        text = response.read().decode(errors="replace")
        return response.status, text, {name.lower(): value for name, value in response.getheaders()}
    except (OSError, http.client.HTTPException):
        return 0, "", {}
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


def first_line(text, width=60):
    return (text.strip().splitlines() or [""])[0][:width]


def wait_ready(gateway, api_key, timeout=90):
    """Wait until the scheduler (GET /nodes) shows both nodes ready; return the seconds waited."""
    start = time.monotonic()
    while True:
        status, text, _ = call("GET", f"{gateway}/nodes", api_key)
        view = {node["id"]: node["status"] for node in json.loads(text)} if status == 200 else {}
        if all(view.get(name) == "ready" for name in NODES):
            return time.monotonic() - start
        if time.monotonic() - start > timeout:
            raise ValueError(f"both nodes were not ready within {timeout} s; scheduler says: {view}")
        time.sleep(0.5)


def holders(api_key, run):
    """Sandbox ID -> node for this run's sandboxes, asking each node directly."""
    query = urllib.parse.urlencode(
        [("metadata", f"repro-run={run}"), ("state", "running"), ("state", "paused")])
    held = {}
    for name, (_, url) in NODES.items():
        status, text, _ = call("GET", f"{url}/v2/sandboxes?{query}", api_key)
        if status != 200:
            raise ValueError(f"listing sandboxes on {name} returned HTTP {status}: {first_line(text, 200)}")
        for sandbox in json.loads(text):
            held[sandbox["sandboxID"]] = name
    return held


def list_through_gateway(gateway, api_key, run, held):
    """List this run's sandboxes with both list requests; return {request: (status, IDs, missing nodes)}."""
    label = urllib.parse.urlencode({"metadata": f"repro-run={run}"})
    requests = {
        "GET /v2/sandboxes": f"/v2/sandboxes?{label}&state=running&state=paused",
        "GET /sandboxes": f"/sandboxes?{label}",
    }
    results = {}
    for name, path in requests.items():
        start = time.monotonic()
        status, text, headers = call("GET", gateway + path, api_key, timeout=60)
        took = time.monotonic() - start
        ids = [item["sandboxID"] for item in json.loads(text)] if status == 200 else None
        missing = headers.get(MISSING_HEADER, "")
        if ids is None:
            shown = repr(first_line(text))
        else:
            shown = ", ".join(f"...{sid[-8:]} on {held.get(sid, '?')}" for sid in ids) or "none"
        print(f"  {name}: HTTP {status} in {took:.2f} s; sandboxes: {shown}"
              + (f"; {MISSING_HEADER}: {missing}" if missing else ""))
        results[name] = (status, ids, missing)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--image", default="ubuntu:22.04", help="OCI image for the sandboxes")
    args = parser.parse_args()
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    gw = args.gateway
    target_vm = NODES[TARGET][0]

    run = uuid.uuid4().hex[:12]
    body = {"image": args.image, "cpuCount": 1, "memoryMB": 512, "timeout": 600,
            "autoPause": False, "metadata": {"repro-run": run}}
    print(f"run {run}: gateway {gw}; node-a = VM agentenv {NODES['node-a'][1]},"
          f" node-b = VM agentenv-2 {NODES['node-b'][1]}")
    print(f"Each sandbox: POST /sandboxes-cold, {args.image}, 1 vCPU, 512 MiB, label repro-run={run}.")

    problems = []
    try:
        in_vm(target_vm, "systemctl start aenv")
        print(f"[{now()}] both nodes ready ({wait_ready(gw, api_key):.1f} s)")

        print("\n== Setup: one sandbox on each node")
        held = {}
        for index in range(1, 5):
            status, text, _ = call("POST", f"{gw}/sandboxes-cold", api_key, body=body, timeout=180)
            if status // 100 != 2:
                raise ValueError(f"create {index} returned HTTP {status}: {first_line(text, 200)}")
            sid = json.loads(text)["sandboxID"]
            held = holders(api_key, run)
            print(f"  create {index}: HTTP {status}, sandbox {sid} on {held.get(sid, '?')}")
            if set(held.values()) == set(NODES):
                break
        if "node-a" not in held.values():
            raise ValueError("no sandbox landed on node-a")
        on_a = sorted(sid for sid, name in held.items() if name == "node-a")

        print("\n== Step 1: both nodes up")
        list_through_gateway(gw, api_key, run, held)

        print(f"\n== Step 2: {TARGET}'s server stopped")
        in_vm(target_vm, "systemctl stop aenv")
        print(f"[{now()}] systemctl stop aenv on {TARGET}: done")
        for name, (status, ids, missing) in list_through_gateway(gw, api_key, run, held).items():
            if status != 200:
                problems.append(f"{name} answered HTTP {status} while only {TARGET} was down")
                continue
            if sorted(ids) != on_a:
                problems.append(f"{name} listed {len(ids)} sandbox(es) instead of node-a's {len(on_a)}")
            if TARGET not in missing.split(","):
                problems.append(f"{name} did not name {TARGET} in {MISSING_HEADER}")

        print(f"\n== Step 3: {TARGET} started again")
        in_vm(target_vm, "systemctl start aenv")
        print(f"[{now()}] {TARGET} ready again ({wait_ready(gw, api_key):.1f} s after the start)")
        status, ids, missing = list_through_gateway(gw, api_key, run, held)["GET /v2/sandboxes"]
        if status != 200 or sorted(ids or []) != sorted(held) or missing:
            problems.append(f"after {TARGET} came back, GET /v2/sandboxes did not list all"
                            f" {len(held)} sandboxes without naming a missing node")
    finally:
        try:
            in_vm(target_vm, "systemctl start aenv")
            wait_ready(gw, api_key)
            left = holders(api_key, run)
            for sid, name in left.items():
                status = call("DELETE", f"{NODES[name][1]}/sandboxes/{sid}", api_key, timeout=60)[0]
                if status != 204:
                    print(f"  cleanup: deleting {sid} on {name} returned HTTP {status}")
            print(f"\n[{now()}] cleanup: deleted {len(left)} sandbox(es) directly on the nodes")
        except ValueError as error:
            print(f"\ncleanup failed: {error}", file=sys.stderr)

    if problems:
        print("\nBUG REPRODUCED: " + "; ".join(problems))
        return 1
    print(f"\nPASS: with {TARGET} down, both lists returned node-a's sandbox and named {TARGET}"
          f" in {MISSING_HEADER}; with it back, the full list returned.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"setup error: {error}", file=sys.stderr)
        sys.exit(2)
