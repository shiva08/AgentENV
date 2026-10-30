#!/usr/bin/env python3
"""Section 4 lab run: Redis goes down for 2 minutes while sandboxes are created.

Uses its own Redis on 127.0.0.1:6380, so the lab's Redis is not touched: the
scheduler and the gateway are restarted with a copy of services/config/lima.json
that points both at it. Creates sandbox A, stops Redis, creates B1 and B2
(round-robin puts one on each node, so both nodes' lists change), then watches
for 2 minutes: GET A, node status, and a create with body {} (the node answers
422, so nothing is created). Starts Redis again (empty, as after a restart
without persistence) and times the recovery. Deletes the sandboxes and restarts
the scheduler and the gateway with lima.json, detached (logs in /tmp).
"""
import json, os, signal, subprocess, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
GW = "http://127.0.0.1:8090"
REDIS_ADDR = "127.0.0.1:6380"
key = subprocess.run(["limactl", "shell", "--workdir", "/", "agentenv", "sudo", "cat",
                      "/var/lib/aenv/secrets/api-key"], capture_output=True, text=True, check=True).stdout.strip()
env = dict(os.environ, AENV_API_KEY=key)
t0 = None


def ts():
    clock = time.strftime("%H:%M:%S")
    return f"{clock} +{time.time() - t0:3.0f} s" if t0 else clock


def request(method, path, body=None, headers=None):
    req = urllib.request.Request(GW + path, method=method, data=body,
                                 headers={"X-API-Key": key, "Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()
    except Exception as err:
        return 0, str(err)


def short(code, text):
    return f"{code} {text.strip()[:32]!r}"


def kill(pattern):
    for pid in subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout.split():
        os.kill(int(pid), signal.SIGTERM)
    time.sleep(1)


def start(binary, config, log):
    return subprocess.Popen([str(REPO / binary), "-config", config], cwd=REPO, env=env,
                            stdout=open(log, "a"), stderr=subprocess.STDOUT, start_new_session=True)


def start_redis(data_dir):
    return subprocess.Popen(["redis-server", "--bind", "127.0.0.1", "--port", REDIS_ADDR.split(":")[1],
                             "--save", "", "--appendonly", "no", "--dir", data_dir],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def nodes():
    code, text = request("GET", "/nodes")
    if code != 200:
        return f"GET /nodes {code}"
    return " ".join(f"{n['id']}={n['status']}" for n in sorted(json.loads(text), key=lambda n: n["id"]))


def create(owner):
    code, text = request("POST", "/sandboxes-cold", json.dumps(
        {"image": "ubuntu:22.04", "cpuCount": 1, "memoryMB": 512, "timeout": 600,
         "metadata": {"owner": owner}}).encode())
    return code, (json.loads(text).get("sandboxID") if code == 201 else text)


cfg = json.load(open(REPO / "services/config/lima.json"))
cfg["scheduler"]["redis_addr"] = REDIS_ADDR
cfg["gateway"]["idempotency_redis_addr"] = REDIS_ADDR
lab_cfg = "/tmp/lab-redis-outage.json"
json.dump(cfg, open(lab_cfg, "w"), indent=2)

data_dir = tempfile.mkdtemp()
open("/tmp/lab-redis-outage-scheduler.log", "w").close()
redis = start_redis(data_dir)
time.sleep(1)
kill("bin/scheduler -config")
kill("bin/gateway -config")
start("services/bin/scheduler", lab_cfg, "/tmp/lab-redis-outage-scheduler.log")
start("services/bin/gateway", lab_cfg, "/tmp/lab-redis-outage-gateway.log")
for _ in range(60):
    if nodes() == "node-a=ready node-b=ready":
        break
    time.sleep(1)
print(f"[{ts()}] scheduler and gateway use Redis {REDIS_ADDR}; {nodes()}")

sandboxes = {}
try:
    code, a = create("redis-outage-a")
    sandboxes["A"] = a
    time.sleep(6)
    print(f"[{ts()}] created A = {a}: HTTP {code}; GET A: {short(*request('GET', f'/sandboxes/{a}'))}")

    redis.terminate()
    redis.wait()
    t0 = time.time()
    print(f"\n== Redis is down")
    print(f"[{ts()}] stopped Redis")
    print(f"[{ts()}] GET A: {short(*request('GET', f'/sandboxes/{a}'))}")
    print(f"[{ts()}] create with an Idempotency-Key: "
          f"{short(*request('POST', '/sandboxes-cold', b'{}', {'Idempotency-Key': f'outage-{t0}'}))}")
    for name in ("B1", "B2"):
        code, sbx = create(f"redis-outage-{name.lower()}")
        if code == 201:
            sandboxes[name] = sbx
        print(f"[{ts()}] create {name} without a key: HTTP {code} {sbx}")
    while time.time() - t0 < 120:
        time.sleep(10)
        print(f"[{ts()}] {nodes():33s} GET A: {short(*request('GET', f'/sandboxes/{a}')):40s} "
              f"create {{}}: {short(*request('POST', '/sandboxes-cold', b'{}'))}")

    redis = start_redis(data_dir)
    up = time.time()
    print(f"\n== Redis is back, empty")
    print(f"[{ts()}] started Redis again")
    pending = {"both nodes ready": None, **{f"GET {name} answers 200": None for name in sandboxes}}
    while time.time() - up < 120 and None in pending.values():
        if pending["both nodes ready"] is None and nodes() == "node-a=ready node-b=ready":
            pending["both nodes ready"] = time.time() - up
        for name, s in sandboxes.items():
            if pending[f"GET {name} answers 200"] is None and request("GET", f"/sandboxes/{s}")[0] == 200:
                pending[f"GET {name} answers 200"] = time.time() - up
        time.sleep(1)
    for what, after in pending.items():
        print(f"  {what}: " + (f"after {after:.0f} s" if after is not None else "not within 120 s"))
finally:
    for s in sandboxes.values():
        print(f"[{ts()}] cleanup: DELETE {s}: HTTP {request('DELETE', f'/sandboxes/{s}')[0]}")
    lines = open("/tmp/lab-redis-outage-scheduler.log").read().splitlines()
    print(f"[{ts()}] scheduler log: {sum('binding reconcile failed' in l for l in lines)} failed heartbeat writes")
    kill("bin/scheduler -config")
    kill("bin/gateway -config")
    redis.terminate()
    start("services/bin/scheduler", "services/config/lima.json", "/tmp/lab-scheduler.log")
    start("services/bin/gateway", "services/config/lima.json", "/tmp/lab-gateway.log")
    print(f"[{ts()}] restarted the scheduler and the gateway with services/config/lima.json (logs /tmp/lab-*.log)")
