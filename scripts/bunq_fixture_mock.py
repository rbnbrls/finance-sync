#!/usr/bin/env python3
"""Small local-only bunq v1 fixture server for the Finance Sync E2E test."""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(os.environ.get("FIXTURE_ROOT", "/fixtures"))


def read_fixture(name: str) -> bytes:
    return (ROOT / name).read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = "bunq-fixture-mock/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.command} {self.path} - {fmt % args}", flush=True)

    def send_json(self, payload: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(b'{"status":"ok"}')
            return
        if path == "/v1/user/9900001/monetary-account":
            self.send_json(read_fixture("monetary-accounts.json"))
            return
        if path == "/v1/monetary-account/9100001/payment":
            self.send_json(read_fixture("payments-account-9100001.json"))
            return
        self.send_json(b'{"Error":[{"error_description":"not found"}]}', 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/v1/session-server":
            self.send_json(read_fixture("session-server.json"))
            return
        self.send_json(b'{"Error":[{"error_description":"not found"}]}', 404)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"bunq fixture mock listening on 0.0.0.0:{port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
