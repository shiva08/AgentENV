#!/usr/bin/env python3
"""Section 10 lab run: Redis loses the scheduler's data while a sandbox runs.

Creates one sandbox through the gateway, then for the old and the new scheduler:
start it, wait 15 s (each node's list applied, then skipped as unchanged),
delete the scheduler's Redis keys (what a Redis restart without persistence
does), and poll GET /sandboxes/{id} for up to 60 s. Deletes the sandbox at the
end; the new scheduler keeps running, detached.

Usage: reproduce-redis-data-loss.py [OLD_SCHEDULER_BINARY]
"""
import json, os, signal, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
GW = "http://127.0.0.1:8090"
OLD = sys.argv[1] if len(sys.argv) > 1 else "/tmp/scheduler-pre-redis-loss"
NEW = str(REPO / "services/bin/scheduler")
key = subprocess.run(["limactl", "shell", "--workdir", "/", "agentenv", "sudo", "cat",
                      "/var/lib/aenv/secrets/api-key"], capture_output=True, text=True, check=True).stdout.strip()


def ts():
    return time.strftime("%H:%M:%S")


def request(method, path, body=None):
    req = urllib.request.Request(GW + path, method=method, data=body,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()
    except Exception as err:
        return 0, str(err)


def redis(*args):
    return subprocess.run(["redis-cli", *args], capture_output=True, text=True, check=True).stdout.split()


def start_scheduler(binary):
    for pid in subprocess.run(["pgrep", "-f", "scheduler.* -config"], capture_output=True, text=True).stdout.split():
        os.kill(int(pid), signal.SIGTERM)
    time.sleep(1)
    subprocess.Popen([binary, "-config", "services/config/lima.json"], cwd=REPO,
                     stdout=open("/tmp/lab-redis-loss-scheduler.log", "a"), stderr=subprocess.STDOUT,
                     start_new_session=True)


start_scheduler(NEW)
time.sleep(8)
code, text = request("POST", "/sandboxes-cold", json.dumps(
    {"image": "ubuntu:22.04", "cpuCount": 1, "memoryMB": 512, "timeout": 600,
     "metadata": {"owner": "redis-loss-run"}}).encode())
sbx = json.loads(text)["sandboxID"]
print(f"[{ts()}] created sandbox {sbx} through the gateway: HTTP {code}")

try:
    for name, binary in (("old scheduler", OLD), ("new scheduler", NEW)):
        print(f"\n== {name} ({binary})")
        start_scheduler(binary)
        time.sleep(15)
        print(f"[{ts()}] started it 15 s ago; each node's list was applied, then skipped as unchanged")
        print(f"[{ts()}] GET {sbx}: HTTP {request('GET', f'/sandboxes/{sbx}')[0]}")
        keys = redis("--scan", "--pattern", "agentenv:scheduler:*")
        redis("DEL", *keys)
        lost = time.time()
        print(f"[{ts()}] Redis lost the scheduler's data: deleted its {len(keys)} keys")
        for _ in range(60):
            code, text = request("GET", f"/sandboxes/{sbx}")
            if code == 200:
                print(f"[{ts()}] GET {sbx}: HTTP 200, {time.time() - lost:.0f} s after the loss")
                break
            time.sleep(1)
        else:
            print(f"[{ts()}] GET {sbx}: still HTTP {code} {text.strip()[:40]!r} 60 s after the loss")
finally:
    code, _ = request("DELETE", f"/sandboxes/{sbx}")
    print(f"\n[{ts()}] cleanup: DELETE {sbx}: HTTP {code}; the new scheduler keeps running (log /tmp/lab-redis-loss-scheduler.log)")
