#!/usr/bin/env python3
"""Show that sandbox bindings are written once and Redis is touched only when a node's list changes.

Uses the two-node Lima setup from docs/usage.md: the gateway on 127.0.0.1:8090,
node-a (VM agentenv) on :8000, node-b (VM agentenv-2) on :8001, the scheduler on
127.0.0.1:9090 with metrics on :9101, and Redis on 127.0.0.1:6379, where the
scheduler keeps the bindings (services/config/lima.json). Nodes send their
sandbox list in every heartbeat.

  1. Restart the scheduler and create two sandboxes through the gateway; show
     each binding in Redis and its time to live.
  2. Steady state: for 30 s, count the lists the scheduler applies and skips as
     unchanged, and the Redis writes.
  3. Restart the scheduler: look the sandboxes up right away (from Redis); each
     node's first list is applied and rewrites nothing.
  4. Restart the server on the first sandbox's node (systemctl restart aenv):
     no binding may be deleted or rewritten.
  5. Write a binding that names a node the scheduler does not know, with a
     live node's address saved in it: the gateway must answer 503 instead of
     sending the request to that address.
  6. Delete the sandboxes through the gateway: the nodes' next heartbeats
     remove their bindings, with no restart.

--steady-only stops after step 2, for a scheduler from before the fix. The
script starts the scheduler itself (--scheduler-bin -config
services/config/lima.json, plus any --env overrides) and leaves it running,
detached, logging to /tmp/verify-bindings-scheduler.log.

Examples:
  python3 docs/proofs/10-reconciliation/scripts/verify-bindings-written-once.py
  python3 docs/proofs/10-reconciliation/scripts/verify-bindings-written-once.py --steady-only \\
      --scheduler-bin /tmp/scheduler-head --env SCHEDULER_BINDING_TTL=30s

Exit status: 0 if every check passed (or --steady-only finished), 1 if a check
failed, 2 on setup errors.
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
CONFIG = "services/config/lima.json"
SCHEDULER_PORT = 9090
SCHEDULER_PATTERN = r"scheduler[^ ]* -config"
SCHEDULER_LOG = "/tmp/verify-bindings-scheduler.log"
METRICS_URL = "http://127.0.0.1:9101/metrics"
REDIS_KEY_PREFIX = "agentenv:scheduler:bindings:sandbox:"
REDIS_WRITES = ["evalsha", "eval", "set", "sadd", "del"]
SANDBOX = {"image": "ubuntu:22.04", "cpuCount": 1, "memoryMB": 512, "timeout": 1200,
           "autoPause": False, "metadata": {"repro": "bindings-written-once"}}


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


def redis(*args):
    try:
        return subprocess.run(["redis-cli", *args], capture_output=True, text=True, check=True).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError(f"redis-cli {' '.join(args)} failed: {error}") from error


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


def restart_scheduler(binary, overrides):
    stop_scheduler()
    env = dict(os.environ, **overrides)
    marker = f"=== {time.time():.3f} started by {os.path.basename(__file__)}"
    with open(SCHEDULER_LOG, "a") as log:
        log.write(f"\n{marker}\n")
        log.flush()
        proc = subprocess.Popen([binary, "-config", CONFIG], cwd=REPO, env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    start = time.monotonic()
    while True:
        with open(SCHEDULER_LOG) as log:
            found = re.search(r'"binding_store":\s*"(\w+)"', log.read().rsplit(marker, 1)[-1])
        if found and port_open(SCHEDULER_PORT):
            return f"pid {proc.pid}, binding store {found.group(1)}"
        if proc.poll() is not None or time.monotonic() - start > 15:
            raise ValueError(f"the scheduler did not start; see {SCHEDULER_LOG}")
        time.sleep(0.2)


def wait_ready(gateway, api_key, timeout=180):
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


def lookup(gateway, api_key, sid, timeout=20):
    """GET the sandbox through the gateway, waiting out its reconnect after a scheduler restart."""
    start = time.monotonic()
    while True:
        status, text = call("GET", f"{gateway}/sandboxes/{sid}", api_key)
        if status in (200, 404) or time.monotonic() - start > timeout:
            return status, first_line(text)
        time.sleep(0.2)


def lists_counted():
    """Heartbeat sandbox lists the scheduler applied or skipped as unchanged since it started."""
    _, text = call("GET", METRICS_URL)
    counts = {"applied": 0, "unchanged": 0}
    for result, value in re.findall(r'agentenv_scheduler_sandbox_lists_total\{result="(\w+)"\} ([0-9.e+]+)', text):
        counts[result] = int(float(value))
    return counts


def redis_writes():
    counts = dict.fromkeys(REDIS_WRITES, 0)
    for name, calls in re.findall(r"cmdstat_(\w+):calls=(\d+)", redis("INFO", "commandstats")):
        if name in counts:
            counts[name] = int(calls)
    return counts


def show(lists, writes):
    if lists is None:
        print("  sandbox lists: in every heartbeat, always applied (this scheduler does not count them)")
    else:
        print(f"  heartbeat sandbox lists: applied {lists['applied']}, skipped as unchanged {lists['unchanged']}")
    scripts = writes["evalsha"] + writes["eval"]
    print(f"  Redis: {scripts} scripts (one per list or record call), SET {writes['set']},"
          f" SADD {writes['sadd']}, DEL {writes['del']}")


def diff(after, before):
    return {key: after[key] - before.get(key, 0) for key in after}


def ttl(sid):
    """Seconds until the sandbox's binding expires, or None if there is none."""
    millis = int(redis("PTTL", REDIS_KEY_PREFIX + sid))
    return None if millis < 0 else millis // 1000


def ttl_text(seconds):
    return "no binding" if seconds is None else f"{seconds} s"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", default="http://127.0.0.1:8090", help="gateway URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--scheduler-bin", default="services/bin/scheduler", help="scheduler binary")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="environment override for the scheduler, e.g. SCHEDULER_BINDING_TTL=30s")
    parser.add_argument("--steady-only", action="store_true", help="stop after the steady-state count")
    args = parser.parse_args()
    api_key = args.api_key or in_vm(NODES["node-a"][0], "cat /var/lib/aenv/secrets/api-key").strip()
    gw = args.gateway
    binary = os.path.join(REPO, args.scheduler_bin)
    overrides = dict(item.split("=", 1) for item in args.env)
    if not os.path.exists(binary):
        raise ValueError(f"{binary} not found; build it with make -C services build")
    if not port_open(6379):
        raise ValueError("Redis is not listening on 127.0.0.1:6379")
    if call("GET", f"{gw}/nodes", api_key)[0] == 0:
        raise ValueError(f"the gateway at {gw} does not answer")

    failed = []

    def check(ok, text):
        print(f"  {'PASS' if ok else 'FAIL'}: {text}")
        if not ok:
            failed.append(text)

    print(f"scheduler: {args.scheduler_bin} -config {CONFIG}"
          + "".join(f" {key}={value}" for key, value in overrides.items()))
    print(f"[{now()}] restarted the scheduler: {restart_scheduler(binary, overrides)}")
    wait_ready(gw, api_key)
    created = {}
    try:
        for _ in range(2):
            status, text = call("POST", f"{gw}/sandboxes-cold", api_key, body=SANDBOX, timeout=180)
            if status // 100 != 2:
                raise ValueError(f"create through the gateway returned HTTP {status}: {first_line(text, 200)}")
            sid = json.loads(text)["sandboxID"]
            created[sid] = next((name for name, (_, url) in NODES.items()
                                 if call("GET", f"{url}/sandboxes/{sid}", api_key)[0] == 200), "?")
            print(f"[{now()}] created {sid} through the gateway on {created[sid]};"
                  f" Redis: {redis('GET', REDIS_KEY_PREFIX + sid)}, expires in {ttl_text(ttl(sid))}")
        first = next(iter(created))

        print(f"\n== Steady state: 30 s with no restarts and no creates")
        lists0, writes0, ttl0 = lists_counted(), redis_writes(), ttl(first)
        time.sleep(30)
        lists1, writes1, ttl1 = lists_counted(), redis_writes(), ttl(first)
        lists = None if args.steady_only else diff(lists1, lists0)
        writes = diff(writes1, writes0)
        show(lists, writes)
        print(f"  binding of {first}: expires in {ttl_text(ttl0)}, 30 s later in {ttl_text(ttl1)}")
        if args.steady_only:
            return 0
        check(lists["applied"] == 0 and lists["unchanged"] > 0, "every list was skipped as unchanged")
        check(writes["set"] == 0 and writes["sadd"] == 0, "no binding was written")
        check(None not in (ttl0, ttl1) and ttl0 - ttl1 >= 25, "the binding was not renewed")

        print(f"\n== Scheduler restart")
        writes0 = redis_writes()
        print(f"[{now()}] restarted the scheduler: {restart_scheduler(binary, overrides)}")
        restarted = time.monotonic()
        for sid in created:
            status, reply = lookup(gw, api_key, sid)
            print(f"[{now()}] GET {sid} through the gateway: HTTP {status} {reply!r}")
            check(status == 200, f"{sid} found right after the scheduler restart")
        time.sleep(max(0.0, 15 - (time.monotonic() - restarted)))
        print(f"[{now()}] 15 s after the restart:")
        lists, writes = lists_counted(), diff(redis_writes(), writes0)
        show(lists, writes)
        check(lists["applied"] == len(NODES), "each node's first list was applied, then skipped")
        check(writes["set"] == 0, "no binding was rewritten")
        print(f"  binding of {first}: expires in {ttl_text(ttl(first))}")

        node = created[first]
        vm, url = NODES[node]
        print(f"\n== Restart of {node}'s server (systemctl restart aenv)")
        lists0, writes0 = lists_counted(), redis_writes()
        in_vm(vm, "systemctl restart aenv")
        print(f"[{now()}] restarted {node}'s server")
        wait_ready(gw, api_key)
        for sid in (sid for sid, holder in created.items() if holder == node):
            status, reply = lookup(gw, api_key, sid)
            print(f"[{now()}] GET {sid} through the gateway: HTTP {status} {reply!r}")
            check(status == 200, f"{sid} found after its node restarted")
        time.sleep(6)
        lists, writes = diff(lists_counted(), lists0), diff(redis_writes(), writes0)
        show(lists, writes)
        check(lists["applied"] == 1, f"{node}'s first list after its restart was applied")
        check(writes["set"] == 0 and writes["del"] == 0, "no binding was deleted or rewritten")
        print(f"  binding of {first}: expires in {ttl_text(ttl(first))}")

        print(f"\n== A binding that names a node the scheduler does not know")
        ghost = "01a17d00-0000-7000-8000-000000000000"
        saved = {"node": {"node_id": "node-gone", "endpoint": url}}
        redis("SET", REDIS_KEY_PREFIX + ghost, json.dumps(saved), "PX", "60000")
        status, reply = call("GET", f"{gw}/sandboxes/{ghost}", api_key)
        redis("DEL", REDIS_KEY_PREFIX + ghost)
        print(f"[{now()}] binding {ghost} -> {json.dumps(saved)}")
        print(f"[{now()}] GET {ghost} through the gateway: HTTP {status} {first_line(reply)!r}")
        check(status == 503 and "node unavailable" in reply, "not sent to the saved address")

        print(f"\n== Delete through the gateway")
        deleted = {}
        for sid in list(created):
            status = call("DELETE", f"{gw}/sandboxes/{sid}", api_key, timeout=60)[0]
            if status // 100 == 2:
                holder = created.pop(sid)
                deleted[sid] = holder
                print(f"[{now()}] DELETE {sid}: HTTP {status}; binding still in Redis:"
                      f" {redis('EXISTS', REDIS_KEY_PREFIX + sid) == '1'}")
                status, reply = lookup(gw, api_key, sid)
                print(f"[{now()}] GET {sid} through the gateway: HTTP {status} {reply!r} (answered by {holder})")
            else:
                print(f"[{now()}] DELETE {sid}: HTTP {status}")
        deleted_at = time.monotonic()
        while any(redis("EXISTS", REDIS_KEY_PREFIX + sid) == "1" for sid in deleted) and time.monotonic() - deleted_at < 30:
            time.sleep(0.5)
        left = [sid for sid in deleted if redis("EXISTS", REDIS_KEY_PREFIX + sid) == "1"]
        print(f"[{now()}] {time.monotonic() - deleted_at:.0f} s later, without restarts: {len(deleted) - len(left)}"
              f" of {len(deleted)} bindings removed by the nodes' next lists")
        check(not left, "the nodes' next heartbeats removed the bindings of deleted sandboxes")
    finally:
        for sid, node in created.items():
            call("DELETE", f"{NODES[node][1]}/sandboxes/{sid}", api_key, timeout=60)
        print(f"\nThe scheduler keeps running, detached; log: {SCHEDULER_LOG}")

    if failed:
        print(f"NOT VERIFIED: {len(failed)} check(s) failed")
        return 1
    print("VERIFIED: bindings were written once and survived both restarts; unchanged lists did not touch"
          " Redis, and the next heartbeat removed the bindings of deleted sandboxes")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
