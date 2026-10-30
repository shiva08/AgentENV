#!/usr/bin/env python3
"""Section 6 lab run: a node whose machine is off, so connects to it time out.

node-b keeps sending heartbeats, so the scheduler lists it as ready, but a
temporary scheduler config gives it the address 10.255.255.1:8001, where
connects time out (no answer, no refusal). Two extra gateways, the old build and
the new one, each get 4 creates with body {}: the node that gets one answers
422, so nothing is created. Stops the temporary scheduler at the end; restart
the lab scheduler with services/config/lima.json afterwards.

Usage: reproduce-connect-timeout.py [OLD_GATEWAY_BINARY]
"""
import json, os, signal, subprocess, sys, time, urllib.request, urllib.error
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
OLD_GW = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gateway-pre-connect-timeout"
NEW_GW = str(REPO / "services/bin/gateway")
SCHED = str(REPO / "services/bin/scheduler")
OFF = "http://10.255.255.1:8001"

key = subprocess.run(["limactl", "shell", "--workdir", "/", "agentenv", "sudo", "cat",
                      "/var/lib/aenv/secrets/api-key"], capture_output=True, text=True, check=True).stdout.strip()
env = dict(os.environ, AENV_API_KEY=key)
base = json.load(open(REPO / "services/config/lima.json"))


def ts():
    return time.strftime("%H:%M:%S")


def write_config(path, mutate):
    cfg = json.loads(json.dumps(base))
    mutate(cfg)
    json.dump(cfg, open(path, "w"), indent=2)
    return path


def node_b_off(cfg):
    for node in cfg["scheduler"]["nodes"]:
        if node["id"] == "node-b":
            node["endpoint"] = OFF


def gateway_on(port, metrics):
    def mutate(cfg):
        cfg["gateway"]["http_listen_addr"] = f"127.0.0.1:{port}"
        cfg["gateway"]["metrics_listen_addr"] = f"127.0.0.1:{metrics}"
    return mutate


def request(method, url, body=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            code, text = resp.status, resp.read().decode()
    except urllib.error.HTTPError as err:
        code, text = err.code, err.read().decode()
    return code, text, time.time() - start


def restart_scheduler(config, log):
    for pid in subprocess.run(["pgrep", "-f", "bin/scheduler -config"], capture_output=True, text=True).stdout.split():
        os.kill(int(pid), signal.SIGTERM)
    time.sleep(1)
    return subprocess.Popen([SCHED, "-config", config], cwd=REPO, stdout=open(log, "w"),
                            stderr=subprocess.STDOUT, start_new_session=True)


def wait_ready(gw):
    for _ in range(60):
        try:
            code, text, _ = request("GET", f"{gw}/nodes")
            if code == 200:
                status = {n["id"]: n["status"] for n in json.loads(text)}
                if status.get("node-a") == "ready" and status.get("node-b") == "ready":
                    return status
        except Exception:
            pass
        time.sleep(1)
    sys.exit("nodes not ready")


off_cfg = write_config("/tmp/lab-node-b-off.json", node_b_off)
print(f"[{ts()}] scheduler config: node-b's address is {OFF}, where connects time out; node-b keeps sending heartbeats")
sched = restart_scheduler(off_cfg, "/tmp/lab-scheduler-off.log")
gateways = []
try:
    for name, binary, port, metrics in (("old gateway", OLD_GW, 8092, 9112), ("new gateway", NEW_GW, 8093, 9113)):
        cfg = write_config(f"/tmp/lab-gateway-{port}.json", gateway_on(port, metrics))
        log = f"/tmp/lab-gateway-{port}.log"
        proc = subprocess.Popen([binary, "-config", cfg], cwd=REPO, env=env, stdout=open(log, "w"),
                                stderr=subprocess.STDOUT, start_new_session=True)
        gateways.append(proc)
        gw = f"http://127.0.0.1:{port}"
        status = wait_ready(gw)
        print(f"\n== {name} ({binary}), {gw}")
        print(f"[{ts()}] scheduler says: " + " ".join(f"{k}={v}" for k, v in sorted(status.items())))
        for i in range(1, 5):
            code, text, took = request("POST", f"{gw}/sandboxes-cold", b"{}")
            print(f"  create {i}: HTTP {code} after {took:.1f} s  {text.strip()[:45]!r}")
        proc.terminate()
        proc.wait()
        lines = [l.strip() for l in open(log) if "10.255.255.1" in l]
        print(f"  gateway log ({len(lines)} lines about node-b):")
        for line in lines:
            print("   ", line[:230])
finally:
    for proc in gateways:
        if proc.poll() is None:
            proc.terminate()
    sched.terminate()
    sched.wait()
    print(f"\n[{ts()}] stopped the temporary scheduler; restart it with services/config/lima.json")
