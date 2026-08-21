"""Serve the light Finance Radar UI with a same-origin, GET-only API proxy."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FRAGMENT = ROOT / "ui_preview" / "finance-radar-ui-concept.html"
ALLOWED_API_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_api_url(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_API_HOSTS:
        raise argparse.ArgumentTypeError("API URL must use http(s) on localhost")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("API URL must not include a path, query, or fragment")
    return value.rstrip("/")


def build_document(fragment: str) -> bytes:
    icon_css = """
      i[data-lucide] { display:inline-grid; width:16px; height:16px; place-items:center; font-style:normal; }
      i[data-lucide=radio]::before { content:'◉'; }
      i[data-lucide=git-branch]::before { content:'⌁'; }
      i[data-lucide=shield-check]::before { content:'◇'; }
      i[data-lucide=search]::before { content:'⌕'; }
      i[data-lucide=chevron-right]::before { content:'›'; }
    """
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy" content="default-src 'self'; connect-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'">
  <title>Finance Radar · 只读 UI 预览</title>
  <style>
    :root {{ color-scheme: light dark; }}
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; min-width: 320px; background: light-dark(#ebe9e3, #05090d); }}
    body {{ padding: 16px; }}
    .sr-only {{ position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }}
    {icon_css}
    @media (max-width: 560px) {{ body {{ padding: 0; }} }}
  </style>
</head>
<body>
{fragment}
</body>
</html>"""
    return document.encode("utf-8")


def create_handler(*, fragment_path: Path, api_url: str) -> type[BaseHTTPRequestHandler]:
    class PreviewHandler(BaseHTTPRequestHandler):
        server_version = "FinanceRadarPreview/1.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            target = urlsplit(self.path)
            if target.path in {"/", "/index.html"}:
                fragment = fragment_path.read_text(encoding="utf-8")
                self._send(HTTPStatus.OK, build_document(fragment), "text/html; charset=utf-8")
                return
            if target.path == "/healthz":
                payload = json.dumps(
                    {"status": "ok", "read_only": True, "api_url": api_url},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(HTTPStatus.OK, payload, "application/json; charset=utf-8")
                return
            if target.path.startswith("/api/v1/"):
                self._proxy_get(target.path, target.query)
                return
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
            self._method_not_allowed()

        def _method_not_allowed(self) -> None:
            self._send(
                HTTPStatus.METHOD_NOT_ALLOWED,
                b'{"error":{"code":"READ_ONLY_PREVIEW","message":"GET requests only"}}',
                "application/json; charset=utf-8",
            )

        def _proxy_get(self, path: str, query: str) -> None:
            upstream = f"{api_url}{path}"
            if query:
                upstream = f"{upstream}?{query}"
            request = Request(upstream, headers={"Accept": "application/json"}, method="GET")
            try:
                with urlopen(request, timeout=15) as response:  # noqa: S310 - URL is loopback-validated
                    body = response.read()
                    status = response.status
                    content_type = response.headers.get("Content-Type", "application/json")
            except HTTPError as exc:
                body = exc.read()
                status = exc.code
                content_type = exc.headers.get("Content-Type", "application/json")
            except URLError as exc:
                body = json.dumps(
                    {
                        "error": {
                            "code": "API_UNAVAILABLE",
                            "message": f"Finance Radar API is unavailable: {exc.reason}",
                        }
                    }
                ).encode("utf-8")
                status = HTTPStatus.BAD_GATEWAY
                content_type = "application/json; charset=utf-8"
            self._send(status, body, content_type)

        def log_message(self, format: str, *args: object) -> None:
            print(f"[{self.log_date_time_string()}] {format % args}")

    return PreviewHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8502)
    parser.add_argument("--api-url", type=validate_api_url, default="http://127.0.0.1:8000")
    parser.add_argument("--fragment", type=Path, default=DEFAULT_FRAGMENT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    fragment_path = args.fragment.resolve()
    if not fragment_path.is_file():
        raise SystemExit(f"UI fragment not found: {fragment_path}")
    handler = create_handler(fragment_path=fragment_path, api_url=args.api_url)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Finance Radar light UI: http://{args.host}:{args.port}")
    print(f"Read-only API proxy: {args.api_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
