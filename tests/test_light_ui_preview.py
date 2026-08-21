from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.serve_light_ui_preview import (
    DEFAULT_FRAGMENT,
    build_document,
    create_handler,
    validate_api_url,
)


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps({"data": {"path": self.path}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def _start_server(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_validate_api_url_accepts_loopback_and_rejects_remote_hosts() -> None:
    assert validate_api_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    assert validate_api_url("http://localhost:8000") == "http://localhost:8000"
    with pytest.raises(Exception):
        validate_api_url("https://example.com")


def test_build_document_wraps_fragment_and_keeps_read_only_csp() -> None:
    document = build_document('<div id="finance-radar-ui-concept">ok</div>').decode("utf-8")
    assert document.startswith("<!doctype html>")
    assert 'connect-src \'self\'' in document
    assert 'id="finance-radar-ui-concept"' in document
    assert "form-action 'none'" in document


def test_preview_serves_ui_proxies_get_and_rejects_post() -> None:
    upstream, upstream_thread = _start_server(_UpstreamHandler)
    api_url = f"http://127.0.0.1:{upstream.server_port}"
    preview_handler = create_handler(fragment_path=DEFAULT_FRAGMENT, api_url=api_url)
    preview, preview_thread = _start_server(preview_handler)
    base = f"http://127.0.0.1:{preview.server_port}"
    try:
        with urlopen(f"{base}/", timeout=5) as response:
            document = response.read().decode("utf-8")
        assert response.status == 200
        assert "Finance Radar · 只读 UI 预览" in document
        assert "connectReadOnlyApi();" in document

        with urlopen(f"{base}/api/v1/events?limit=3", timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["data"]["path"] == "/api/v1/events?limit=3"

        request = Request(f"{base}/api/v1/events", data=b"{}", method="POST")
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 405
        assert json.loads(error.value.read())["error"]["code"] == "READ_ONLY_PREVIEW"
    finally:
        preview.shutdown()
        upstream.shutdown()
        preview_thread.join(timeout=5)
        upstream_thread.join(timeout=5)
