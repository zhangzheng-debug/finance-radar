from __future__ import annotations

from datetime import datetime, timezone

from app.web.components import (
    EVENT_KEYBOARD_JS,
    SAVED_FLOW_HTML,
    SAVED_FLOW_JS,
    adjacent_event_id,
    age_label,
    event_keyboard_payload,
    event_button_label,
    event_anchor_id,
    event_feed_row,
    evidence_route_markup,
    evidence_summary,
    command_palette_markup,
    facet_counts,
    facet_values,
    family_option_label,
    flow_shortcuts_markup,
    market_context_items,
    market_context_markup,
    market_horizon_items,
    public_event_copy,
    public_event_quality,
    public_event_state,
    score_dimensions,
    source_health_state,
    terminal_search_state,
    saved_flow_payload,
    saved_public_flow_payload,
    source_option_label,
)


def test_event_feed_row_is_compact_linked_and_html_safe() -> None:
    row = event_feed_row(
        {
            "event_id": "event/a?x=1",
            "status": "verified",
            "event_family": "enforcement",
            "event_type": "sec_litigation_release",
            "company_name": "A&B <Holdings>",
            "last_updated_at": "2026-07-18T12:34:00+00:00",
            "credibility_tier": "P0",
            "discovery_source": "sec_current_filings",
            "evidence_excerpt": "Exact <passage> & source.",
        }
    )
    assert "event%2Fa%3Fx%3D1" in row
    assert "preview_flow=%E5%85%A8%E9%83%A8%E4%BA%8B%E4%BB%B6" in row
    assert "preview_event_id=event%2Fa%3Fx%3D1" in row
    assert "Event_Intelligence" not in row
    assert "A&amp;B &lt;Holdings&gt;" in row
    assert "Exact &lt;passage&gt; &amp; source." in row
    assert "status-verified" in row
    assert "◆" in row
    assert "authority-p0" in row
    assert "当前页预览" in row
    assert 'target="_self"' in row
    assert 'target="_blank"' not in row
    assert "监管执法" in row
    assert "<script" not in row
    assert f'id="{event_anchor_id("event/a?x=1")}"' in row


def test_public_event_feed_row_hides_internal_codes_and_bounds_excerpt() -> None:
    row = event_feed_row(
        {
            "event_id": "public-event",
            "status": "candidate",
            "event_family": "debt_financing",
            "event_type": "internal_classifier_slug",
            "company_name": "Example Issuer",
            "last_updated_at": "2026-08-03T22:34:00+00:00",
            "credibility_tier": "P?",
            "discovery_source": "sec_current_filings",
            "evidence_excerpt": "A" * 500,
        },
        public=True,
    )

    assert "待核验" in row
    assert "债务融资" in row
    assert "SEC 官方文件" in row
    assert "查看证据" in row
    assert "REVIEW" not in row
    assert "P?" not in row
    assert "sec_current_filings" not in row
    assert "internal classifier slug" not in row
    assert "A" * 20 not in row
    assert "仍需核对原始文件" in row
    assert "为什么关注" in row


def test_public_event_feed_row_marks_session_changes_without_internal_details() -> None:
    row = event_feed_row(
        {
            "event_id": "changed-public-event",
            "status": "candidate",
            "event_family": "listing_status",
            "company_name": "Changed Example",
            "_changed_since_view": True,
        },
        public=True,
    )
    assert "自上次查看有更新" in row
    assert "is-changed" in row


def test_public_flow_shortcuts_use_reader_facing_labels() -> None:
    markup = flow_shortcuts_markup(
        {"verified": 5, "candidate": 4, "weak": 2, "rejected": 1},
        public=True,
    )

    assert "待核验" in markup
    assert "已粗审" in markup
    assert "已核验" in markup
    assert "已排除" in markup
    assert "待复核" not in markup
    assert "已拒绝" not in markup
    assert "preview_state=pending_verification" in markup
    assert 'target="_self"' in markup
    assert 'target="_blank"' not in markup


def test_public_flow_shortcuts_use_partitioned_funnel_counts() -> None:
    markup = flow_shortcuts_markup(
        {"verified": 5, "candidate": 4, "weak": 0, "rejected": 1},
        public=True,
        public_funnel={
            "total": 12,
            "verified": 5,
            "excluded": 1,
            "insufficient": 2,
            "rough_reviewed": 3,
            "pending_verification": 1,
        },
    )
    assert "在当前页面筛选证据不足信息流，2条" in markup
    assert "在当前页面筛选已粗审信息流，3条" in markup
    assert '<span class="flow-count">12</span>' in markup


def test_public_event_copy_never_promotes_raw_english_boilerplate() -> None:
    event = {
        "status": "candidate",
        "public_state": "rough_reviewed",
        "event_family": "capital_structure",
        "company_name": "Example Ltd.",
        "discovery_source": "sec_current_filings",
        "evidence_excerpt": "THIS WARRANT AGREEMENT contains raw legal boilerplate.",
    }
    copy = public_event_copy(event)
    assert public_event_state(event) == "rough_reviewed"
    assert copy["state_label"] == "已粗审"
    assert "资本结构" in copy["summary"]
    assert "THIS WARRANT" not in copy["summary"]
    assert "正式证据核验" in copy["summary"]


def test_public_event_copy_prefers_structured_fact_and_keeps_state_boundary() -> None:
    copy = public_event_copy(
        {
            "status": "candidate",
            "public_state": "insufficient",
            "event_family": "listing_status",
            "company_name": "Example Ltd.",
            "facts": {
                "evidence_summary": "交易所公告称该公司收到上市合规通知。",
            },
            "evidence_excerpt": "UNRELATED RAW ENGLISH EXCERPT",
        }
    )

    assert copy["summary_provenance"] == "结构化事实摘要"
    assert "交易所公告称该公司收到上市合规通知" in copy["summary"]
    assert "当前证据不足" in copy["summary"]
    assert "UNRELATED RAW ENGLISH EXCERPT" not in copy["summary"]


def test_public_event_copy_does_not_invent_an_event_from_subject_and_family() -> None:
    event = {
        "status": "candidate",
        "public_state": "pending_verification",
        "event_family": "listing_status",
        "ticker_at_event": "ICX",
        "event_type": "listing_status",
    }

    copy = public_event_copy(event)
    quality = public_event_quality(event, [])

    assert "ICX" in copy["summary"]
    assert "上市状态" in copy["summary"]
    assert "尚没有可公开复述的主体—动作—阶段事实" in copy["summary"]
    assert "出现一项" not in copy["summary"]
    assert copy["summary_provenance"] == "发现线索说明"
    assert quality["reader_ready"] is False
    assert quality["gaps"] == [
        "缺少主体—动作—阶段事实摘要",
        "缺少可定位的原文段落",
    ]


def test_public_event_quality_requires_subject_fact_and_citable_passage() -> None:
    event = {
        "company_name": "Example Ltd.",
        "facts": {"evidence_summary": "交易所公告称该公司收到上市合规通知并说明了整改期限。"},
    }
    evidence = [
        {
            "evidence_url": "https://example.test/original",
            "evidence_passage": "The exchange notice names Example Ltd. and states the exact compliance deadline.",
        }
    ]

    quality = public_event_quality(event, evidence)

    assert quality["reader_ready"] is True
    assert quality["gaps"] == []
    assert quality["citable_evidence_count"] == 1


def test_public_event_row_labels_its_distinct_time_clocks() -> None:
    row = event_feed_row(
        {
            "event_id": "timed-public-event",
            "status": "candidate",
            "event_family": "listing_status",
            "company_name": "Timed Example",
            "event_date": "2026-08-02",
            "first_seen_at": "2026-08-03T01:02:00+00:00",
            "last_updated_at": "2026-08-03T02:03:00+00:00",
            "reviewed_at": "2026-08-03T03:04:00+00:00",
        },
        public=True,
    )

    for label in ("最后更新", "事件日", "系统发现", "核验记录"):
        assert label in row
    assert "2026-08-02" in row


def test_evidence_route_is_truthful_about_shadow_and_human_review() -> None:
    markup = evidence_route_markup(
        {"verified": 5, "candidate": 4, "weak": 2, "rejected": 1},
        review_queue=6,
    )
    assert "规范化事件" in markup
    assert ">12<" in markup
    assert "候选与弱证据" in markup
    assert ">6<" in markup
    assert "只分流" in markup
    assert "不替代人工结论" in markup
    assert "自动执行始终禁用" in markup


def test_flow_shortcuts_link_to_named_views_and_show_counts() -> None:
    markup = flow_shortcuts_markup(
        {"verified": 5, "candidate": 4, "weak": 2, "rejected": 1}
    )
    assert "快速信息流" in markup
    assert "preview_flow=%E5%BE%85%E5%A4%8D%E6%A0%B8" in markup
    assert "preview_flow=%E5%B7%B2%E6%A0%B8%E9%AA%8C" in markup
    assert "在当前页面筛选待复核信息流，4条" in markup
    assert "Event_Intelligence" not in markup
    assert '<span class="flow-count">12</span>' in markup


def test_terminal_search_state_normalizes_query_and_clears_stale_flow() -> None:
    assert terminal_search_state("  Alpha   TST  ") == {
        "flow": "全部事件",
        "q": "Alpha TST",
        "limit": "50",
    }


def test_saved_flow_payload_normalizes_and_bounds_browser_only_state() -> None:
    assert saved_flow_payload(
        "已核验",
        "  enforcement   action ",
        "  SEC   NVDA ",
        50,
        source="  sec_current_filings  ",
    ) == {
        "scope": "reviewer",
        "flow": "已核验",
        "family": "enforcement action",
        "source": "sec_current_filings",
        "q": "SEC NVDA",
        "limit": "50",
    }
    fallback = saved_flow_payload("not-a-flow", "x" * 100, "y" * 140, 999, source="z" * 100)
    assert fallback["flow"] == "待复核"
    assert fallback["limit"] == "25"
    assert len(fallback["family"]) == 80
    assert len(fallback["source"]) == 80
    assert len(fallback["q"]) == 120


def test_saved_flow_component_is_device_local_bounded_and_safe() -> None:
    assert 'finance-radar.saved-flows.${currentScope}.v2' in SAVED_FLOW_JS
    assert "我的信息流" in SAVED_FLOW_HTML
    assert "const maxFlows = 8" in SAVED_FLOW_JS
    assert 'url.searchParams.delete("event_id")' in SAVED_FLOW_JS
    assert 'url.searchParams.set("source", config.source)' in SAVED_FLOW_JS
    assert "localStorage.setItem" in SAVED_FLOW_JS
    assert "textContent = item.name" in SAVED_FLOW_JS
    assert "fetch(" not in SAVED_FLOW_JS
    assert "XMLHttpRequest" not in SAVED_FLOW_JS
    assert 'aria-live="polite"' in SAVED_FLOW_HTML
    assert 'aria-label="本机保存的信息流"' in SAVED_FLOW_HTML


def test_public_saved_flow_payload_keeps_only_bounded_research_filters() -> None:
    assert saved_public_flow_payload(
        "pending_verification",
        "  earnings  ",
        "  ACME   filing ",
        "最近 24 小时",
        "latest",
        24,
        source=" sec ",
    ) == {
        "scope": "public",
        "state": "pending_verification",
        "family": "earnings",
        "source": "sec",
        "q": "ACME filing",
        "period": "最近 24 小时",
        "sort": "latest",
        "page_size": "24",
    }


def test_facets_preserve_deep_links_and_render_data_backed_safe_commands() -> None:
    facets = {
        "families": [
            {"value": "enforcement", "count": 12},
            {"value": "<script>alert(1)</script>", "count": 2},
        ],
        "sources": [{"value": "sec current/filings", "count": 8}],
    }
    assert facet_values(facets, "families", "new_family") == [
        "",
        "new_family",
        "enforcement",
        "<script>alert(1)</script>",
    ]
    counts = facet_counts(facets, "families")
    assert family_option_label("enforcement", counts) == "监管执法 · enforcement · 12"
    assert source_option_label("sec current/filings", facet_counts(facets, "sources")) == (
        "sec current/filings · 8"
    )
    markup = command_palette_markup(facets)
    assert "快捷命令" in markup
    assert "family=enforcement" in markup
    assert "source=sec%20current%2Ffilings" in markup
    assert 'target="_self"' in markup
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in markup
    assert "<script>" not in markup


def test_market_context_uses_latest_snapshot_and_exposes_freshness_without_trading() -> None:
    now = datetime(2026, 7, 18, 12, 10, tzinfo=timezone.utc)
    detail = {
        "assets": [
            {
                "asset_id": "asset-1",
                "symbol": "ETH",
                "provider_symbol": "ETH/USD",
                "venue": "provider registry",
                "market_observation_allowed": 1,
            }
        ],
        "market_snapshots": [
            {
                "asset_id": "asset-1",
                "provider": "binance_public",
                "provider_symbol": "ETHUSDT",
                "price": "2010.25",
                "currency": "USDT",
                "captured_at": "2026-07-18T12:05:00Z",
                "read_only": 1,
                "no_trading": 1,
            }
        ],
    }
    items = market_context_items(detail, now=now)
    assert items[0]["price"] == "2010.25 USDT"
    assert items[0]["freshness"] == "CAPTURED 5M"
    assert items[0]["state"] == "ok"
    markup = market_context_markup(detail, now=now)
    assert "binance_public" in markup
    assert "只读事件后观察" in markup
    assert "下单" not in markup


def test_market_context_is_honest_and_html_safe_when_snapshot_is_missing() -> None:
    detail = {
        "assets": [
            {
                "asset_id": "asset-1",
                "symbol": "A&B<script>",
                "venue": "Twelve<Data",
                "market_observation_allowed": 1,
            }
        ],
        "market_snapshots": [],
    }
    markup = market_context_markup(detail)
    assert "UNAVAILABLE" in markup
    assert "不会用零值或旧行情替代" in markup
    assert "A&amp;B&lt;script&gt;" in markup
    assert "<script>" not in markup


def test_market_horizons_distinguish_observed_pending_and_missed() -> None:
    detail = {
        "market_jobs": [
            {"asset_id": "asset-1", "observation_window": "t_plus_5m", "status": "COMPLETED"},
            {"asset_id": "asset-1", "observation_window": "t_plus_30m", "status": "PENDING"},
            {"asset_id": "asset-1", "observation_window": "t_plus_1d", "status": "MISSED_WINDOW"},
        ],
        "market_metrics": [
            {
                "stable_id": "asset-1",
                "metric_name": "observer_return_t_plus_5m_pct__ETHUSDT",
                "metric_value": "5.123456",
            }
        ],
    }
    items = market_horizon_items(detail, "asset-1")
    assert [(item["label"], item["value"]) for item in items] == [
        ("T+5M", "+5.12%"),
        ("T+30M", "PENDING"),
        ("T+1D", "MISSED"),
    ]
    assert items[0]["state"] == "evidence"
    assert items[2]["state"] == "risk"


def test_adjacent_event_navigation_is_bounded_and_recovers_unknown_current() -> None:
    ids = ["event-a", "event-b", "event-c"]
    assert adjacent_event_id(ids, "event-b", -1) == "event-a"
    assert adjacent_event_id(ids, "event-b", 1) == "event-c"
    assert adjacent_event_id(ids, "event-a", -1) == "event-a"
    assert adjacent_event_id(ids, "event-c", 1) == "event-c"
    assert adjacent_event_id(ids, "missing", 1) == "event-b"
    assert adjacent_event_id([], "missing", 1) == "missing"


def test_keyboard_navigation_uses_structured_v2_data_and_ignores_editing() -> None:
    payload = event_keyboard_payload(["event-a", "</script>"], "event-a")
    assert payload == {
        "event_ids": ["event-a", "</script>"],
        "selected_id": "event-a",
        "search_label": "全局检索",
    }
    assert 'input[aria-label="${searchLabel}"]' in EVENT_KEYBOARD_JS
    assert '["INPUT", "TEXTAREA", "SELECT"].includes(tag)' in EVENT_KEYBOARD_JS
    assert 'key === "j"' in EVENT_KEYBOARD_JS
    assert 'key === "k"' in EVENT_KEYBOARD_JS


def test_event_button_label_is_compact_and_identifies_workflow() -> None:
    label = event_button_label(
        {
            "status": "candidate",
            "last_updated_at": "2026-07-18T12:34:00+00:00",
            "ticker_at_event": "TST",
            "company_name": "A company name that is intentionally much longer than the row budget",
        }
    )
    assert "REVIEW" in label
    assert "TST" in label
    assert len(label) < 90


def test_age_label_uses_operational_units() -> None:
    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    assert age_label("2026-07-18T11:59:42Z", now=now) == "18s"
    assert age_label("2026-07-18T11:40:00Z", now=now) == "20m"
    assert age_label("2026-07-17T12:00:00Z", now=now) == "1d"


def test_evidence_dimensions_keep_conflict_and_model_separate() -> None:
    evidence = [
        {"authority_tier": "P2", "evidence_status": "discovery"},
        {"authority_tier": "P0", "evidence_status": "contradicted_by_primary"},
    ]
    summary = evidence_summary(evidence)
    assert summary["highest_authority"] == "P0"
    assert summary["conflict"] is True
    dimensions = score_dimensions(
        {"status": "candidate", "current_version": 2},
        evidence,
        {"label": "ABSTAIN", "confidence": 0.61},
    )
    values = {label: (value, state) for label, value, state in dimensions}
    assert values["风险路由"] == ("ABSTAIN", "watch")
    assert values["证据冲突"] == ("DETECTED", "risk")
    assert values["最高权威"] == ("P0", "evidence")
    assert values["新颖性"][0] == "REVISION"
    assert values["模型"][0] == "61% · SHADOW"


def test_source_health_errors_sort_before_watch_and_ok() -> None:
    error = source_health_state({"cursor_status": "FAILED", "last_error": "timeout"})
    watch = source_health_state({"cursor_status": "UNOBSERVED"})
    ok = source_health_state({"cursor_status": "SUCCESS", "last_success_at": "2026-07-18T12:00:00Z"})
    assert error == (0, "ERROR")
    assert watch == (1, "WATCH")
    assert ok == (2, "OK")
