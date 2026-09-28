# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Serve deterministic fake-device samples inside the isolated test network."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class MetricsHandler(BaseHTTPRequestHandler):
    """Expose only the fixture metrics endpoint."""

    def do_GET(self) -> None:
        """Serve a Prometheus text response or a missing-path error."""
        if self.path != "/metrics":
            self.send_error(404)
            return
        body = Path(__file__).with_name("metrics.prom").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("", int(os.getenv("PROMETHEUS_PORT", "8090"))), MetricsHandler).serve_forever()
