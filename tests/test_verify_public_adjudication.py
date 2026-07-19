from __future__ import annotations

import json
from pathlib import Path

import httpx

from scripts.verify_public_adjudication import verify


def test_public_adjudication_acceptance_requires_read_only_boundary(tmp_path: Path) -> None:
    diagnostics = tmp_path / "ui.json"
    diagnostics.write_text(
        json.dumps(
            {
                "title": "Adjudication Studio · Finance Radar",
                "final_url": "https://example.test/radar/Adjudication_Studio",
                "body_text_length": 900,
                "skeleton_count": 0,
                "page_errors": [],
                "http_errors": [],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/v1/health"):
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.1",
                    "data": {
                        "status": "ok",
                        "operations": {"schema_version": 3},
                        "capabilities": ["dual_blind_adjudication"],
                    },
                },
            )
        if request.url.path.endswith("/api/v1/adjudication/status"):
            return httpx.Response(
                200,
                json={
                    "schema_version": "1.1",
                    "data": {
                        "status": "NOT_READY_FOR_FREEZE",
                        "samples": 24,
                        "status_counts": {"OPEN": 24},
                        "valid_annotations": 0,
                        "public_review_ui_default_closed": True,
                        "production_changed": False,
                        "blind_v2_frozen": False,
                    },
                },
            )
        if request.url.path.endswith("/api/v1/adjudication/queue"):
            return httpx.Response(
                403,
                json={"error": {"code": "ADMIN_TOKEN_REQUIRED", "message": "required"}},
            )
        if request.url.path.endswith("/radar/Adjudication_Studio"):
            return httpx.Response(200, text="streamlit shell")
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = verify(
            "https://example.test/finance-radar-api",
            "https://example.test/radar/Adjudication_Studio",
            diagnostics,
            client=client,
        )
    assert report["status"] == "PASS"
    assert report["passed"] == report["total"] == 11
    assert report["boundaries"]["admin_token_used"] is False
    assert report["boundaries"]["review_submitted"] is False
