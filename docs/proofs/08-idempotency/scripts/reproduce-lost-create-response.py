#!/usr/bin/env python3
"""Reproduce a duplicate sandbox caused by a lost create response and a retry.

The agent sends POST /sandboxes-cold through a local proxy that injects a
network fault: it forwards the first request, waits for the server's reply,
then drops that reply and resets the connection. The agent retries, as most
HTTP clients do, and the server creates a second sandbox for the same
logical request.

Example (AgentENV reachable at http://127.0.0.1:8000, e.g. in a Lima VM):
  export AENV_API_KEY="$(limactl shell agentenv sudo cat /var/lib/aenv/secrets/api-key)"
  python3 docs/proofs/08-idempotency/scripts/reproduce-lost-create-response.py

With --idempotency-key the agent sends one Idempotency-Key for the logical
create on every attempt; a gateway with idempotency enabled then replays the
first result instead of creating a second sandbox.

Exit status: 1 if the retry created a duplicate (bug reproduced), 0 if exactly
one sandbox exists, 2 on setup errors. Sandboxes from the run are deleted
unless --keep is given; they also expire after --sandbox-timeout seconds.
"""

import argparse
import http.client
import json
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.parse
import uuid

SANDBOX_ID = re.compile(rb'"sandboxID"\s*:\s*"([^"]+)"')


class Api:
    def __init__(self, base_url, api_key, timeout):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" or not parts.hostname:
            raise ValueError(f"expected an http:// URL, got {base_url!r}")
        self.host, self.port = parts.hostname, parts.port or 80
        self.api_key, self.timeout = api_key, timeout

    def call(self, method, path, body=None, extra_headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"X-API-Key": self.api_key, "Connection": "close", **(extra_headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            payload = response.read()
        finally:
            conn.close()
        return response.status, json.loads(payload) if payload else None, response.headers


def read_request(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("client closed before sending a full request")
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(body) < length:
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("client closed before sending the request body")
        body += chunk
    return head + b"\r\n\r\n" + body


def read_to_eof(sock):
    chunks = []
    while chunk := sock.recv(65536):
        chunks.append(chunk)
    return b"".join(chunks)


class LostResponseProxy:
    """Relays requests to the server, but drops the reply to the first one."""

    def __init__(self, upstream):
        self.upstream = upstream
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        drop = True
        while True:
            client, _ = self.listener.accept()
            threading.Thread(target=self._relay, args=(client, drop), daemon=True).start()
            drop = False

    def _relay(self, client, drop):
        with client:
            try:
                request = read_request(client)
                # The agent sends Connection: close, so the server ends the reply with EOF.
                with socket.create_connection(self.upstream) as upstream:
                    upstream.sendall(request)
                    reply = read_to_eof(upstream)
            except OSError as error:
                print(f"proxy: relay failed: {error}")
                return
            if not drop:
                client.sendall(reply)
                return
            status = reply.split(b"\r\n", 1)[0].decode(errors="replace")
            found = SANDBOX_ID.search(reply)
            sandbox = found.group(1).decode() if found else "unknown"
            print(f"proxy: server answered '{status}' for sandbox {sandbox}; "
                  "dropping the reply and resetting the connection")
            # Close with RST, as a broken network path would.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


def create_with_retries(agent, body, attempts, idempotency_key):
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            status, payload, response_headers = agent.call("POST", "/sandboxes-cold", body, headers)
        except (OSError, http.client.HTTPException) as error:
            print(f"agent: attempt {attempt} got no response after "
                  f"{time.monotonic() - started:.0f}s ({type(error).__name__}); retrying")
            time.sleep(1)
            continue
        if status != 201:
            raise ValueError(f"create failed: HTTP {status}: {payload}")
        replayed = " (replayed)" if response_headers.get("Idempotent-Replayed") == "true" else ""
        print(f"agent: attempt {attempt} succeeded after {time.monotonic() - started:.0f}s; "
              f"sandbox {payload['sandboxID']}{replayed}")
        return payload["sandboxID"]
    raise ValueError(f"no create attempt succeeded in {attempts} tries")


def list_run(server, query):
    status, listed, _ = server.call("GET", f"/v2/sandboxes?{query}")
    if status != 200:
        raise ValueError(f"listing sandboxes failed: HTTP {status}: {listed}")
    return [sandbox["sandboxID"] for sandbox in listed]


def delete_run(server, query):
    try:
        ids = list_run(server, query)
        for sandbox_id in ids:
            status, _, _ = server.call("DELETE", f"/sandboxes/{sandbox_id}")
            if status != 204:
                print(f"cleanup: deleting {sandbox_id} returned HTTP {status}")
        print(f"cleanup: deleted {len(ids)} sandbox(es)")
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f"cleanup failed: {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="AgentENV server URL")
    parser.add_argument("--api-key", default=os.environ.get("AENV_API_KEY"),
                        help="API key (default: $AENV_API_KEY)")
    parser.add_argument("--image", default="ubuntu:22.04", help="OCI image for the sandbox")
    parser.add_argument("--attempts", type=int, default=3, help="create attempts the agent makes")
    parser.add_argument("--http-timeout", type=float, default=600,
                        help="seconds per request; a cold start may pull the image")
    parser.add_argument("--sandbox-timeout", type=int, default=600, help="sandbox lifetime in seconds")
    parser.add_argument("--keep", action="store_true", help="keep the sandboxes for inspection")
    parser.add_argument("--idempotency-key", action="store_true",
                        help="send one Idempotency-Key for the create on every attempt")
    parser.add_argument("--nodes", default="",
                        help="comma-separated node URLs; show which node holds each sandbox, "
                             "e.g. http://127.0.0.1:8000,http://127.0.0.1:8001")
    args = parser.parse_args()
    if not args.api_key:
        raise ValueError("set AENV_API_KEY or pass --api-key")

    server = Api(args.url, args.api_key, args.http_timeout)
    run = uuid.uuid4().hex[:12]
    query = urllib.parse.urlencode({"metadata": f"repro-run={run}"})
    try:
        list_run(server, query)
    except OSError as error:
        raise ValueError(f"cannot reach {args.url}: {error}") from error

    proxy = LostResponseProxy((server.host, server.port))
    agent = Api(f"http://127.0.0.1:{proxy.port}", args.api_key, args.http_timeout)
    body = {
        "image": args.image,
        "cpuCount": 1,
        "memoryMB": 512,
        "timeout": args.sandbox_timeout,
        "autoPause": False,
        "metadata": {"repro-run": run},
    }
    idempotency_key = str(uuid.uuid4()) if args.idempotency_key else None
    print(f"run {run}: the agent asks for ONE sandbox from {args.image}"
          + (f" with Idempotency-Key {idempotency_key}" if idempotency_key else ""))
    try:
        known = create_with_retries(agent, body, args.attempts, idempotency_key)
        ids = list_run(server, query)
        print(f"server: {len(ids)} sandbox(es) labelled repro-run={run}: {', '.join(ids)}")
        for url in filter(None, args.nodes.split(",")):
            held = list_run(Api(url, args.api_key, 15), query)
            print(f"node {url} holds: {', '.join(held) or 'none'}")
        if len(ids) > 1:
            print(f"BUG REPRODUCED: one logical create produced {len(ids)} sandboxes; "
                  f"the agent only knows about {known}")
            return 1
        print("PASS: the retry did not create a duplicate sandbox")
        return 0
    finally:
        if args.keep:
            print(f"kept sandboxes; they expire after {args.sandbox_timeout}s")
        else:
            delete_run(server, query)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
