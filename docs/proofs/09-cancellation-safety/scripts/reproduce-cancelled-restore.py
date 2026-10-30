#!/usr/bin/env python3
"""Show that one cancelled restore breaks a snapshot on a node until the server restarts.

Uses node-a (VM agentenv, http://127.0.0.1:8000) from docs/usage.md, Docker and
the S3 server image the repository's own tests use (Silo). Steps:

  0. Setup: start the S3 server on 127.0.0.1:9000 with a bucket, start a TCP
     proxy in front of it on 127.0.0.1:9002, and point node-a's snapshot store
     at S3 through the proxy (repository_backend = "oss"). node-a's config is
     backed up first and restored at the end.
  1. Create a sandbox on node-a, snapshot it to S3 as S, delete the sandbox.
  2. The proxy holds every download of S's vm_state.bin. Launch S on node-a
     (POST /sandboxes with templateID S) and give up after --client-timeout
     seconds, as a client, or the gateway after its request timeout, does when
     a request takes too long.
  3. The proxy stops holding. Launch S again, twice.
  4. Restart node-a's server and launch S once more.

Each sandbox is ubuntu:22.04 with 1 vCPU and 512 MiB, labelled
repro-run=<run>. At the end, also after Ctrl-C or an error, the script deletes
them and S, restores node-a's config and removes the S3 server.

Example:
  python3 docs/proofs/09-cancellation-safety/scripts/reproduce-cancelled-restore.py

Exit status: 1 if a launch in step 3 failed (bug reproduced), 0 if both
worked, 2 on setup errors.
"""

import argparse
import asyncio
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

VM = "agentenv"
NODE = "http://127.0.0.1:8000"
NODE_CONFIG = "/var/lib/aenv/config/config.toml"
CONFIG_BACKUP = NODE_CONFIG + ".repro-cancel-backup"
S3_IMAGE = "pgsty/silo:RELEASE.2026-09-16T00-00-00Z"
S3_CONTAINER = "aenv-silo-repro"
S3_PORT = 9000
PROXY_PORT = 9002
BUCKET = "aenv-repro-cancel"
S3_USER = "minioadmin"
REQUEST_LINE = re.compile(rb"(GET|HEAD|PUT|POST|DELETE) (\S+) HTTP/1\.[01]\r\n")


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


def run_cmd(*args, timeout=120):
    try:
        result = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise ValueError(f"{args[0]} not found") from error
    if result.returncode != 0:
        raise ValueError(f"{' '.join(args[:3])} failed: {(result.stderr or result.stdout).strip()[-500:]}")
    return result.stdout


def in_vm(script):
    """Run a shell script as root inside node-a's VM and return its output."""
    return run_cmd("limactl", "shell", "--workdir", "/", VM, "sudo", "sh", "-c", script, timeout=120)


def now():
    return time.strftime("%H:%M:%S")


def message(text, width=300):
    try:
        text = json.loads(text).get("message", text)
    except (ValueError, AttributeError):
        pass
    return text.strip().replace("\n", " ")[:width]


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_serving(timeout=120):
    start = time.monotonic()
    while call("GET", f"{NODE}/health", timeout=3)[0] // 100 != 2:
        if time.monotonic() - start > timeout:
            raise ValueError(f"node-a did not answer /health within {timeout} s")
        time.sleep(0.5)


def restart_node():
    in_vm("systemctl restart aenv")
    wait_serving()


def restore_config():
    """Put node-a's original config back if a backup exists; True if it did."""
    restored = in_vm(f"if [ -e {CONFIG_BACKUP} ]; then mv {CONFIG_BACKUP} {NODE_CONFIG}; echo restored; fi").strip()
    if restored:
        restart_node()
    return bool(restored)


class HoldingProxy:
    """TCP proxy to the S3 server that can hold the answers to vm_state.bin downloads."""

    def __init__(self, listen_port, target_port):
        self.target_port = target_port
        self.hold = False
        self.events = []
        self.loop = asyncio.new_event_loop()
        started = threading.Event()
        failure = []

        def serve():
            asyncio.set_event_loop(self.loop)
            try:
                self.loop.run_until_complete(asyncio.start_server(self._connection, "127.0.0.1", listen_port))
            except OSError as error:
                failure.append(error)
                started.set()
                return
            started.set()
            self.loop.run_forever()

        threading.Thread(target=serve, daemon=True).start()
        started.wait(5)
        if failure:
            raise ValueError(f"cannot listen on 127.0.0.1:{listen_port}: {failure[0]}")

    def note(self, what):
        self.events.append((time.monotonic(), what))

    async def _connection(self, node_reader, node_writer):
        try:
            s3_reader, s3_writer = await asyncio.open_connection("127.0.0.1", self.target_port)
        except OSError:
            node_writer.close()
            return
        state = {"vm_state": False, "holding": False}
        await asyncio.gather(self._requests(node_reader, s3_writer, state),
                             self._answers(s3_reader, node_writer, state))

    async def _requests(self, reader, writer, state):
        try:
            while data := await reader.read(65536):
                for match in REQUEST_LINE.finditer(data):
                    state["vm_state"] = match.group(1) == b"GET" and b"/vm_state.bin" in match.group(2)
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            if state["holding"]:
                self.note("node-a closed the held download: it stopped waiting for it")
            writer.close()

    async def _answers(self, reader, writer, state):
        try:
            while data := await reader.read(65536):
                if state["vm_state"] and self.hold:
                    state["holding"] = True
                    self.note("node-a is downloading vm_state.bin from S3; the proxy holds the answer")
                    while self.hold and not reader.at_eof():
                        await asyncio.sleep(0.05)
                    state["holding"] = False
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()


def start_s3():
    run_cmd("docker", "run", "-d", "--rm", "--name", S3_CONTAINER, "-p", f"127.0.0.1:{S3_PORT}:9000",
            "-e", f"MINIO_ROOT_USER={S3_USER}", "-e", f"MINIO_ROOT_PASSWORD={S3_USER}",
            S3_IMAGE, "server", "/data")
    start = time.monotonic()
    while call("GET", f"http://127.0.0.1:{S3_PORT}/minio/health/live", timeout=2)[0] != 200:
        if time.monotonic() - start > 60:
            raise ValueError("the S3 server did not become ready within 60 s")
        time.sleep(0.5)
    run_cmd("curl", "-sSf", "--aws-sigv4", "aws:amz:us-east-1:s3", "--user", f"{S3_USER}:{S3_USER}",
            "-X", "PUT", f"http://127.0.0.1:{S3_PORT}/{BUCKET}")


def use_s3_through_proxy():
    in_vm(f"""set -e
cp -p {NODE_CONFIG} {CONFIG_BACKUP}
sed -i 's/^repository_backend *=.*/repository_backend = "oss"/' {NODE_CONFIG}
cat >> {NODE_CONFIG} <<'EOF'

[backend.oss]
endpoint = "http://host.lima.internal:{PROXY_PORT}"
bucket = "{BUCKET}"
region = "us-east-1"
access_key_id = "{S3_USER}"
access_key_secret = "{S3_USER}"
EOF""")
    restart_node()


def launch(api_key, body, timeout):
    start = time.monotonic()
    status, text = call("POST", f"{NODE}/sandboxes", api_key, body=body, timeout=timeout)
    return status, text, time.monotonic() - start


def run_sandboxes(api_key, run):
    query = urllib.parse.urlencode([("state", "running"), ("state", "paused"), ("metadata", f"repro-run={run}")])
    status, text = call("GET", f"{NODE}/v2/sandboxes?{query}", api_key)
    return [sandbox["sandboxID"] for sandbox in json.loads(text)] if status == 200 else []


def cleanup(api_key, run, snapshot):
    for sid in run_sandboxes(api_key, run):
        status = call("DELETE", f"{NODE}/sandboxes/{sid}", api_key, timeout=60)[0]
        print(f"[{now()}] cleanup: delete sandbox {sid}: HTTP {status}")
    if snapshot:
        status = call("DELETE", f"{NODE}/snapshots/{snapshot}", api_key, timeout=60)[0]
        print(f"[{now()}] cleanup: delete snapshot {snapshot}: HTTP {status}")
    try:
        if restore_config():
            print(f"[{now()}] cleanup: restored node-a's config and restarted its server")
    except ValueError as error:
        print(f"[{now()}] cleanup: could not restore node-a's config: {error}")
    if subprocess.run(["docker", "rm", "-f", S3_CONTAINER], capture_output=True, check=False).returncode == 0:
        print(f"[{now()}] cleanup: removed the S3 server")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY, else read from node-a's VM)")
    parser.add_argument("--image", default="ubuntu:22.04", help="OCI image for the source sandbox")
    parser.add_argument("--client-timeout", type=float, default=3.0,
                        help="seconds the client waits for the launch in step 2")
    args = parser.parse_args()
    api_key = args.api_key or in_vm("cat /var/lib/aenv/secrets/api-key").strip()
    if restore_config():
        print(f"[{now()}] restored node-a's config left behind by an interrupted run")
    if call("GET", f"{NODE}/health")[0] // 100 != 2:
        raise ValueError("node-a does not answer /health")
    for port in (S3_PORT, PROXY_PORT):
        if port_open(port):
            raise ValueError(f"something already listens on 127.0.0.1:{port}")

    run = uuid.uuid4().hex[:12]
    labels = {"repro-run": run}
    print(f"run {run}: node-a = VM {VM} {NODE}; S3 server on 127.0.0.1:{S3_PORT}, which node-a reaches"
          f" through a proxy on 127.0.0.1:{PROXY_PORT}")
    print(f"Each launch: POST /sandboxes on node-a with templateID S, label repro-run={run}.")

    snapshot = None
    try:
        print("\n== Setup")
        start_s3()
        proxy = HoldingProxy(PROXY_PORT, S3_PORT)
        print(f"[{now()}] started the S3 server (Docker container {S3_CONTAINER}) with bucket {BUCKET},"
              f" and the proxy")
        use_s3_through_proxy()
        print(f"[{now()}] node-a keeps snapshots in S3 now, through the proxy (repository_backend = \"oss\");"
              f" its config is backed up in {CONFIG_BACKUP}")

        print("\n== Step 1: a snapshot S in S3")
        body = {"image": args.image, "cpuCount": 1, "memoryMB": 512, "timeout": 1800,
                "autoPause": False, "metadata": labels}
        status, text = call("POST", f"{NODE}/sandboxes-cold", api_key, body=body, timeout=180)
        if status != 201:
            raise ValueError(f"create returned HTTP {status}: {message(text)}")
        source = json.loads(text)["sandboxID"]
        status, text = call("POST", f"{NODE}/sandboxes/{source}/snapshots", api_key, body={}, timeout=600)
        if status != 201:
            raise ValueError(f"snapshot returned HTTP {status}: {message(text)}")
        snapshot = json.loads(text)["snapshotID"]
        call("DELETE", f"{NODE}/sandboxes/{source}", api_key, timeout=60)
        print(f"[{now()}] created sandbox {source}, saved snapshot S = {snapshot} to S3, deleted the sandbox")

        launch_body = {"templateID": snapshot, "timeout": 600, "autoPause": False, "metadata": labels}
        print("\n== Step 2: the client gives up while node-a downloads S")
        proxy.hold = True
        print(f"[{now()}] the proxy now holds node-a's downloads of vm_state.bin")
        start = time.monotonic()
        status, text, took = launch(api_key, launch_body, args.client_timeout)
        if status:
            print(f"[{now()}] launch S, client timeout {args.client_timeout:g} s: HTTP {status} after {took:.1f} s:"
                  f" {message(text)!r}")
            raise ValueError("the launch answered before the client gave up; nothing was cancelled")
        print(f"[{now()}] launch S, client timeout {args.client_timeout:g} s: no answer, the client gave up"
              f" after {took:.1f} s")
        deadline = time.monotonic() + 5
        while not any("closed" in what for _, what in proxy.events) and time.monotonic() < deadline:
            time.sleep(0.1)
        for at, what in proxy.events:
            print(f"  proxy, {at - start:4.1f} s after the launch: {what}")
        proxy.hold = False
        time.sleep(3)
        left = run_sandboxes(api_key, run)
        print(f"[{now()}] the proxy no longer holds anything; sandboxes on node-a from this run:"
              f" {', '.join(left) or 'none'}")

        print("\n== Step 3: launch S again, with nothing held")
        failed = 0
        for attempt in (1, 2):
            status, text, took = launch(api_key, launch_body, 300)
            if status == 201:
                sid = json.loads(text)["sandboxID"]
                call("DELETE", f"{NODE}/sandboxes/{sid}", api_key, timeout=60)
                print(f"  launch {attempt}: HTTP 201 after {took:.1f} s, sandbox {sid} (deleted)")
            else:
                failed += 1
                print(f"  launch {attempt}: HTTP {status} after {took:.1f} s: {message(text)!r}")

        print("\n== Step 4: restart node-a's server, then launch S")
        restart_node()
        print(f"[{now()}] restarted node-a's server (systemctl restart aenv)")
        status, text, took = launch(api_key, launch_body, 300)
        relaunched = status == 201
        if relaunched:
            sid = json.loads(text)["sandboxID"]
            call("DELETE", f"{NODE}/sandboxes/{sid}", api_key, timeout=60)
            print(f"  launch: HTTP 201 after {took:.1f} s, sandbox {sid} (deleted)")
        else:
            print(f"  launch: HTTP {status} after {took:.1f} s: {message(text)!r}")
        print()
    finally:
        cleanup(api_key, run, snapshot)

    print("\n== Result")
    if failed:
        print(f"BUG REPRODUCED: after one cancelled launch, {failed} of 2 later launches of S on node-a failed;"
              + (" S launched again only after node-a's server restarted." if relaunched
                 else " S still failed after a server restart."))
        return 1
    print("PASS: launches of S worked after the cancelled one.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
