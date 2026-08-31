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
    public_event_evidence_posture,
    public_event_quality,
    public_event_risk_assessment,
    public_event_source_provenance,
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
            "citation_ready": False,
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
            "display_headline": "SEC filing describes a financing update",
            "headline_mode": "ATTRIBUTED_SOURCE",
            "risk_assessment": {
                "route": "RISK_REVIEW",
                "confidence": 0.82,
                "confidence_applicable": True,
                "model_version": "risk-router-test-v1",
                "decision_source": "TRAINED_SEMANTIC_MODEL",
                "shadow": True,
                "current": True,
            },
            "evidence_excerpt": "A" * 500,
        },
        public=True,
    )

    assert "来源已收录" not in row
    assert "来源摘录" not in row
    assert "自动风险语义" not in row
    assert "risk-router-test-v1" not in row
    assert "债务融资" in row
    assert "SEC 官方文件" in row
    assert "查看 ›" in row
    assert "REVIEW" not in row
    assert "P?" not in row
    assert "sec_current_filings" not in row
    assert "internal classifier slug" not in row
    assert "A" * 20 not in row
    assert "SEC filing describes a financing update" in row
    assert "尚未" not in row
    assert "为什么关注" not in row


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
    assert "本次浏览后有更新" in row
    assert "is-changed" not in row


def test_public_event_feed_marks_only_the_current_event_as_selected() -> None:
    selected = event_feed_row(
        {
            "event_id": "event-a",
            "status": "candidate",
            "event_family": "company_governance",
            "company_name": "Selected Example",
            "display_headline": "Selected event headline",
        },
        public=True,
        selected_event_id="event-a",
    )
    ordinary = event_feed_row(
        {
            "event_id": "event-b",
            "status": "candidate",
            "event_family": "company_governance",
            "company_name": "Ordinary Example",
            "display_headline": "Ordinary event headline",
        },
        public=True,
        selected_event_id="event-a",
    )

    assert 'class="feed-row public-feed-row is-selected"' in selected
    assert 'aria-current="true"' in selected
    assert "is-selected" not in ordinary
    assert "aria-current" not in ordinary


def test_public_flow_shortcuts_do_not_expose_reviewer_workflow_labels() -> None:
    markup = flow_shortcuts_markup(
        {"verified": 5, "candidate": 4, "weak": 2, "rejected": 1},
        public=True,
    )

    assert "全部事件" in markup
    assert "待核验" not in markup
    assert "已粗审" not in markup
    assert "已核验" not in markup
    assert "已排除" not in markup
    assert "待复核" not in markup
    assert "已拒绝" not in markup
    assert "preview_state=" not in markup
    assert 'target="_self"' in markup
    assert 'target="_blank"' not in markup


def test_public_flow_shortcuts_only_report_total_inventory() -> None:
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
    assert "在当前页面筛选全部事件信息流，12条" in markup
    assert "证据不足" not in markup
    assert "已粗审" not in markup
    assert '<span class="flow-count">12</span>' in markup


def test_public_event_copy_never_promotes_raw_english_boilerplate() -> None:
    event = {
        "status": "candidate",
        "public_state": "rough_reviewed",
        "citation_ready": False,
        "evidence_posture": "PRIMARY_SOURCE_AVAILABLE",
        "event_family": "capital_structure",
        "company_name": "Example Ltd.",
        "discovery_source": "sec_current_filings",
        "evidence_excerpt": "THIS WARRANT AGREEMENT contains raw legal boilerplate.",
    }
    copy = public_event_copy(event)
    assert public_event_state(event) == "rough_reviewed"
    assert copy["evidence_label"] == "一手来源"
    assert "资本结构" in copy["headline"]
    assert copy["summary"] == ""
    assert "THIS WARRANT" not in copy["headline"]
    assert "已粗审" not in copy["headline"]


def test_public_event_copy_labels_capture_excerpt_and_ignores_private_fallbacks() -> None:
    copy = public_event_copy(
        {
            "status": "candidate",
            "public_state": "pending_verification",
            "citation_ready": False,
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
            "event_family": "listing_status",
            "company_name": "Example Ltd.",
            "facts": {
                "fact_summary": "REVIEWER_PRIVATE_FACT",
                "evidence_summary": "INTERNAL_DETECTOR_REASON",
            },
            "unverified_capture_excerpt": "The source API reported a listing item.",
        }
    )

    assert copy["summary_provenance"] == "来源文本"
    assert copy["headline_mode"] == "ATTRIBUTED_SOURCE"
    assert copy["headline"] == "The source API reported a listing item."
    assert copy["summary"] == ""
    assert "REVIEWER_PRIVATE_FACT" not in copy["headline"]
    assert "INTERNAL_DETECTOR_REASON" not in copy["headline"]


def test_public_event_copy_localizes_provider_names() -> None:
    copy = public_event_copy(
        {
            "citation_ready": False,
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
            "discovery_source": "sharadar_active_research",
            "display_headline": "A source-attributed event description",
            "headline_mode": "ATTRIBUTED_SOURCE",
            "headline_source": "Sharadar active historical discovery",
        }
    )
    assert copy["headline_source"] == "历史研究资料"


def test_public_event_copy_suppresses_structured_fact_until_citation_ready() -> None:
    copy = public_event_copy(
        {
            "status": "candidate",
            "public_state": "insufficient",
            "citation_ready": False,
            "evidence_posture": "PRIMARY_SOURCE_AVAILABLE",
            "event_family": "listing_status",
            "company_name": "Example Ltd.",
            "facts": {
                "public_fact_summary": "交易所公告称该公司收到上市合规通知。",
            },
            "evidence_excerpt": "UNRELATED RAW ENGLISH EXCERPT",
        }
    )

    assert copy["summary_provenance"] == "事件记录"
    assert "交易所公告称该公司收到上市合规通知" not in copy["headline"]
    assert copy["evidence_label"] == "一手来源"
    assert copy["summary"] == ""
    assert "UNRELATED RAW ENGLISH EXCERPT" not in copy["headline"]


def test_public_event_copy_renders_structured_fact_only_when_primary_supported() -> None:
    copy = public_event_copy(
        {
            "status": "candidate",
            "public_state": "pending_verification",
            "citation_ready": True,
            "evidence_posture": "PRIMARY_SUPPORTED",
            "event_family": "listing_status",
            "company_name": "Example Ltd.",
            "facts": {
                "public_fact_summary": "交易所公告称该公司收到上市合规通知。",
            },
        }
    )

    assert copy["summary_provenance"] == "结构化事实摘要"
    assert "交易所公告称该公司收到上市合规通知" in copy["headline"]
    assert copy["summary"] == ""
    assert copy["evidence_label"] == "原文支持"


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

    assert "ICX" in copy["headline"]
    assert "上市状态" in copy["headline"]
    assert copy["headline_mode"] == "RECORD"
    assert copy["summary"] == ""
    assert copy["summary_provenance"] == "事件记录"
    assert quality["reader_ready"] is False
    assert quality["gaps"] == [
        "缺少主体—动作—阶段事实摘要",
        "缺少可定位的原文段落",
    ]


def test_public_evidence_posture_prefers_contract_and_uses_conservative_fallbacks() -> None:
    explicit = public_event_evidence_posture(
        {
            "evidence_posture": "PRIMARY_SOURCE_AVAILABLE",
            "citation_ready": False,
            "evidence_gap_codes": [
                "MISSING_CITABLE_EVIDENCE",
                "NO_CAPTURED_SOURCE",
            ],
        }
    )
    assert explicit["label"] == "一手来源"
    assert explicit["gap_labels"] == ["可引用原文待补", "来源捕获待补"]

    # A source registry name alone proves neither a successful capture nor an
    # available original document.
    no_source = public_event_evidence_posture(
        {"discovery_source": "sec_current_filings", "citation_ready": False}
    )
    assert no_source["key"] == "NO_SOURCE"
    assert no_source["label"] == "事件记录"

    captured = public_event_evidence_posture(
        {"captured_source_count": 1, "citation_ready": False}
    )
    assert captured["key"] == "SOURCE_CAPTURED"


def test_public_source_provenance_separates_access_from_claim_citation() -> None:
    linked = public_event_source_provenance(
        {
            "citation_ready": True,
            "source_provenance": {"access": "CLAIM_SOURCE_LINKED"},
        }
    )
    assert linked["label"] == "原文支持"
    assert linked["status_class"] == "verified"

    primary = public_event_source_provenance(
        {
            "citation_ready": False,
            "evidence_posture": "SOURCE_CAPTURED",
            "source_provenance": {
                "classification_version": "public-source-provenance-v1",
                "access": "PRIMARY_SOURCE",
            },
        }
    )
    assert primary["label"] == "一手来源"
    assert primary["status_class"] == "source-primary"

    publisher = public_event_source_provenance(
        {
            "citation_ready": False,
            "public_source_url_count": 1,
            "captured_source_count": 1,
        }
    )
    assert publisher["label"] == "来源可查"
    assert publisher["status_class"] == "source-public"

    capture = public_event_source_provenance(
        {"citation_ready": False, "captured_text_count": 1}
    )
    assert capture["label"] == "来源已保存"
    assert capture["status_class"] == "source-capture"

    filtered_only = public_event_source_provenance(
        {
            "citation_ready": False,
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
            "displayable_source_count": 0,
        }
    )
    assert filtered_only["key"] == "NO_PUBLIC_SOURCE"


def test_public_risk_assessment_handles_missing_and_shadow_outputs_honestly() -> None:
    waiting = public_event_risk_assessment({"risk_assessment": None})
    assert waiting["label"] == ""
    assert waiting["explanation"] == ""
    assert waiting["current"] is False

    shadow = public_event_risk_assessment(
        {
            "risk_assessment": {
                "route": "RISK_REVIEW",
                "confidence": 0.836,
                "confidence_applicable": True,
                "model_version": "risk-router-v4",
                "decision_source": "TRAINED_SEMANTIC_MODEL",
                "shadow": True,
                "current": True,
            }
        }
    )
    assert shadow["label"] == ""
    assert shadow["confidence"] == ""
    assert shadow["current"] is False
    assert shadow["explanation"] == ""

    unapproved_semantic = public_event_risk_assessment(
        {
            "semantic_assessment": {
                "polarity": "ADVERSE",
                "materiality": "MATERIAL_ADVERSE",
                "adverse_strength": "HIGH",
                "semantic_priority": "PRIORITY_REVIEW",
                "assessment_scope": "SOURCE_CONDITIONAL",
                "current": True,
            }
        }
    )
    assert unapproved_semantic["label"] == ""
    assert unapproved_semantic["current"] is False

    qwen = public_event_risk_assessment(
        {
            "semantic_assessment": {
                "polarity": "ADVERSE",
                "materiality": "MATERIAL_ADVERSE",
                "adverse_strength": "HIGH",
                "semantic_priority": "PRIORITY_REVIEW",
                "assessment_scope": "SOURCE_CONDITIONAL",
                "publication_state": "PUBLIC_APPROVED",
                "training_basis": "INDEPENDENT_DUAL_HUMAN_GOLD",
                "automatic": True,
                "shadow": False,
                "no_trading": True,
                "confirms_event_fact": False,
                "current": True,
            }
        }
    )
    assert qwen["label"] == "负面 · 强度高"
    assert qwen["polarity_label"] == "负面"
    assert qwen["materiality_label"] == "重大负面"
    assert qwen["strength_label"] == "高"
    assert qwen["explanation"] == "基于来源文本的风险语义判断。"
    assert qwen["basis_label"] == "基于来源文本"
    assert qwen["confidence"] == ""
    assert qwen["decision_source_label"] == "千问混合语义模型"

    ai_consensus = public_event_risk_assessment(
        {
            "semantic_assessment": {
                "polarity": "POSITIVE",
                "materiality": "NOT_MATERIAL_ADVERSE",
                "adverse_strength": "NONE",
                "semantic_priority": "ROUTINE",
                "assessment_scope": "EVIDENCE_SUPPORTED",
                "publication_state": "PUBLIC_APPROVED",
                "training_basis": "DUAL_REVIEW_AI_CONSENSUS",
                "automatic": True,
                "shadow": False,
                "no_trading": True,
                "confirms_event_fact": False,
                "current": True,
            }
        }
    )
    assert ai_consensus["label"] == "正面"
    assert ai_consensus["materiality_label"] == "非重大负面"
    assert ai_consensus["strength_label"] == "无"
    assert ai_consensus["current"] is True


def test_public_risk_assessment_hides_internal_rules_fallback_and_unknown_source() -> None:
    evidence_gate = public_event_risk_assessment(
        {
            "risk_assessment": {
                "route": "ABSTAIN",
                "confidence": 1.0,
                # A stale/malformed producer flag still must not expose a rule
                # score as if it were calibrated model confidence.
                "confidence_applicable": True,
                "model_version": "router-package-v4",
                "decision_source": "DETERMINISTIC_EVIDENCE_GATE",
                "shadow": True,
                "current": True,
            }
        }
    )
    assert evidence_gate["heading"] == "研究信号"
    assert evidence_gate["label"] == ""
    assert evidence_gate["confidence"] == ""
    assert evidence_gate["model_version"] == ""
    assert evidence_gate["decision_source"] == ""
    assert evidence_gate["explanation"] == ""
    assert "影子模型" not in evidence_gate["label"]
    assert "影子模型" not in evidence_gate["explanation"]

    semantic_gate = public_event_risk_assessment(
        {
            "risk_assessment": {
                "route": "NON_TARGET",
                "confidence": 0.99,
                "confidence_applicable": True,
                "decision_source": "DETERMINISTIC_SEMANTIC_POLICY_GATE",
                "shadow": False,
                "current": True,
            }
        }
    )
    assert semantic_gate["label"] == ""
    assert semantic_gate["confidence"] == ""
    assert "确定性语义规则门" not in semantic_gate["explanation"]

    keyword = public_event_risk_assessment(
        {
            "risk_assessment": {
                "route": "RISK_REVIEW",
                "confidence": 0.91,
                "confidence_applicable": True,
                "model_version": "fallback-v1",
                "decision_source": "KEYWORD_FALLBACK",
                "shadow": True,
                "current": True,
            }
        }
    )
    assert keyword["label"] == ""
    assert keyword["confidence"] == ""
    assert keyword["model_version"] == ""
    assert "关键词" not in keyword["explanation"]

    unknown = public_event_risk_assessment(
        {
            "risk_assessment": {
                "route": "ABSTAIN",
                "confidence": 0.75,
                "confidence_applicable": True,
                "decision_source": "MODEL",
                "shadow": True,
                "current": True,
            }
        }
    )
    assert unknown["label"] == ""
    assert unknown["confidence"] == ""
    assert "MODEL" not in unknown["explanation"]
    assert "影子模型" not in unknown["label"]


def test_public_feed_does_not_expose_internal_rule_gate_output() -> None:
    row = event_feed_row(
        {
            "event_id": "evidence-gated",
            "status": "candidate",
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
            "risk_assessment": {
                "route": "ABSTAIN",
                "confidence_applicable": False,
                "decision_source": "DETERMINISTIC_EVIDENCE_GATE",
                "shadow": True,
                "current": True,
            },
        },
        public=True,
    )
    assert "自动风险分流" not in row
    assert "证据规则门" not in row
    assert "自动弃权" not in row
    assert "影子模型" not in row


def test_public_card_never_exposes_legacy_workflow_disposition() -> None:
    ordinary = event_feed_row(
        {
            "event_id": "legacy-verified",
            "status": "verified",
            "public_state": "verified",
            "evidence_posture": "SOURCE_CAPTURED",
            "captured_source_count": 1,
        },
        public=True,
    )
    assert "来源已收录" not in ordinary
    assert "来源摘录" not in ordinary
    assert "来源已保存" in ordinary
    assert ">已核验<" not in ordinary

    excluded = event_feed_row(
        {
            "event_id": "legacy-excluded",
            "status": "rejected",
            "public_state": "excluded",
            "evidence_posture": "NO_SOURCE",
        },
        public=True,
    )
    assert "来源异常" not in excluded
    assert "status-source-none" in excluded
    assert ">已排除<" not in excluded


def test_public_event_quality_requires_subject_fact_and_citable_passage() -> None:
    event = {
        "company_name": "Example Ltd.",
        "facts": {
            "public_fact_summary": "交易所公告称该公司收到上市合规通知并说明了整改期限。",
            "claim_subject": "Example Ltd.",
            "claim_action": "listing_compliance_notice",
            "claim_stage": "DISCLOSED",
            "known_at": "2026-08-20T01:02:03+00:00",
        },
    }
    evidence = [
        {
            "evidence_url": "https://example.test/original",
            "evidence_passage": "The exchange notice names Example Ltd. and states the exact compliance deadline.",
            "evidence_status": "machine_extracted_unreviewed",
            "relation_status": "SCOPED_MATCH",
            "subject_match": 1,
            "event_claim_supported": 1,
            "date_coherent": 1,
            "authority_tier": "P0_official",
            "reader_eligible": 1,
        }
    ]

    quality = public_event_quality(event, evidence)

    assert quality["reader_ready"] is True
    assert quality["gaps"] == []
    assert quality["citable_evidence_count"] == 1


def test_public_event_quality_requires_strict_dual_human_receipt() -> None:
    event = {
        "company_name": "Example Ltd.",
        "facts": {
            "public_fact_summary": "Example Ltd. disclosed that its chief financial officer resigned.",
            "claim_subject": "Example Ltd.",
            "claim_action": "chief financial officer resigned",
            "claim_stage": "DISCLOSED",
            "known_at": "2026-08-20T01:02:03+00:00",
        },
    }
    evidence = {
        "evidence_url": "https://www.sec.gov/example",
        "evidence_passage": (
            "Example Ltd. disclosed that its chief financial officer resigned "
            "effective immediately."
        ),
        "evidence_status": "accepted_dual_human_primary_evidence",
        "relation_status": "HUMAN_CONFIRMED",
        "subject_match": 1,
        "event_claim_supported": 1,
        "date_coherent": 1,
        "authority_tier": "P0",
        "dual_human_receipt_consistent": 1,
        "reader_eligible": 1,
    }

    assert public_event_quality(event, [evidence])["reader_ready"] is True
    evidence["dual_human_receipt_consistent"] = 0
    quality = public_event_quality(event, [evidence])
    assert quality["reader_ready"] is False
    assert quality["citable_evidence_count"] == 0


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

    for label in ("最后更新", "事件日"):
        assert label in row
    assert "系统发现" not in row
    assert "人工复核记录" not in row
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
                "metric_name": "reaction_return_t_plus_5m_pct__ETHUSDT",
                "metric_value": "5.123456",
            }
        ],
    }
    items = market_horizon_items(detail, "asset-1")
    assert [(item["label"], item["value"]) for item in items] == [
        ("T+5M", "+5.12%"),
        ("T+30M", "PENDING"),
        ("T+2H", "NOT SCHEDULED"),
        ("下个收盘", "NOT SCHEDULED"),
        ("T+1D", "MISSED"),
        ("T+5D", "NOT SCHEDULED"),
    ]
    assert items[0]["state"] == "evidence"
    assert items[4]["state"] == "risk"


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
