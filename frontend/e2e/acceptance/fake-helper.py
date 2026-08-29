#!/usr/bin/env python3
"""Tiny fake helper process for acceptance testing of the supervision protocol.

Speaks the same HTTP interface as llama-server:
  GET /health      -> 200 {"status": "ok"}
  GET /props       -> 200 {"model_path": "<model>"}
  GET /v1/models   -> 200 {"data": [{"id": "<model>"}]}

Exits cleanly on SIGTERM. Supports modes via command-line flags:
  --port PORT          Port to listen on (required)
  --model MODEL        Model name for /props (default: "test-model")
  --hang-health        /health blocks forever (tests unhealthy detection)
  --fail-health        /health returns 500 (tests health-check failure)
  --slow-start SECS    Delay before health becomes ready
"""

import argparse
import json
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Event

shutdown_event = Event()
ready_event = Event()
mode_hang_health = False
mode_fail_health = False
model_name = "test-model"


class HelperHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress request logging

    def do_GET(self):
        if self.path == "/health":
            if mode_hang_health:
                shutdown_event.wait()
                return
            if mode_fail_health or not ready_event.is_set():
                self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "not ready"}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
            return

        if self.path == "/props":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"model_path": model_name}).encode())
            return

        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": model_name, "object": "model", "owned_by": "acceptance-fixture"}
                        ],
                    }
                ).encode()
            )
            return

        self.send_response(404)
        self.end_headers()


def main():
    global mode_hang_health, mode_fail_health, model_name

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default="test-model")
    parser.add_argument("--hang-health", action="store_true")
    parser.add_argument("--fail-health", action="store_true")
    parser.add_argument("--slow-start", type=float, default=0)
    args = parser.parse_args()

    model_name = args.model
    mode_hang_health = args.hang_health
    mode_fail_health = args.fail_health

    def handle_term(signum, frame):
        shutdown_event.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_term)

    server = HTTPServer(("127.0.0.1", args.port), HelperHandler)
    server.timeout = 1

    # Delayed readiness (simulates model loading)
    if args.slow_start > 0:
        start = time.monotonic()
        while time.monotonic() - start < args.slow_start:
            server.handle_request()
        ready_event.set()
    else:
        ready_event.set()

    while not shutdown_event.is_set():
        server.handle_request()


if __name__ == "__main__":
    main()
