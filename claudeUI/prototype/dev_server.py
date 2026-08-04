"""Serve the UI prototype with a local read-only Finance Radar API proxy."""

from __future__ import annotations

import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import requests


DEFAULT_UPSTREAM = "https://radar.18-208-34-152.sslip.io:8443/finance-radar-api"
API_PREFIX = "/finance-radar-api/api/v1/"


class ReadOnlyPreviewHandler(SimpleHTTPRequestHandler):
    upstream = DEFAULT_UPSTREAM

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path.startswith(API_PREFIX):
            self._proxy_read_only(include_body=True)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path.startswith(API_PREFIX):
            self._proxy_read_only(include_body=False)
            return
        super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802 - explicit hard boundary
        self.send_error(405, "Read-only preview: POST is disabled")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def _proxy_read_only(self, *, include_body: bool) -> None:
        suffix = self.path[len("/finance-radar-api") :]
        target = f"{self.upstream}{suffix}"
        try:
            response = requests.get(
                target,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "FinanceRadar-UI-Preview/1.0",
                },
                timeout=20,
            )
            payload = response.content
            self.send_response(response.status_code)
            self.send_header(
                "Content-Type",
                response.headers.get("Content-Type", "application/json"),
            )
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Preview-Proxy", "read-only")
            self.end_headers()
            if include_body:
                self.wfile.write(payload)
        except requests.RequestException as error:
            self.send_error(502, f"Read-only upstream unavailable: {error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--upstream",
        default=os.environ.get("FINANCE_RADAR_API_ORIGIN", DEFAULT_UPSTREAM),
        help="Finance Radar API base ending in /finance-radar-api",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ReadOnlyPreviewHandler.upstream = args.upstream.rstrip("/")
    server = ThreadingHTTPServer((args.host, args.port), ReadOnlyPreviewHandler)
    print(f"Finance Radar UI preview: http://{args.host}:{args.port}/index.html")
    print(f"Read-only API upstream: {ReadOnlyPreviewHandler.upstream}")
    server.serve_forever()


if __name__ == "__main__":
    main()
