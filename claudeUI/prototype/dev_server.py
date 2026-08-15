"""Serve the archived frozen Finance Radar prototype without any API proxy."""

from __future__ import annotations

import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class ArchivedPrototypeHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(Path(__file__).parent), **kwargs)

    def do_POST(self) -> None:  # noqa: N802 - explicit archive boundary
        self.send_error(405, "Archived prototype: mutations are disabled")

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ArchivedPrototypeHandler)
    print(f"Finance Radar UI preview: http://{args.host}:{args.port}/index.html")
    print("Archived frozen prototype: no API proxy or production-data connection")
    server.serve_forever()


if __name__ == "__main__":
    main()
