from pathlib import Path


TERMINAL = (
    Path(__file__).resolve().parents[1]
    / "claudeUI"
    / "prototype"
    / "index.html"
)


def terminal_source() -> str:
    return TERMINAL.read_text(encoding="utf-8")


def test_archived_prototype_is_frozen_and_clears_stale_detail() -> None:
    source = terminal_source()
    assert "ARCHIVED · FROZEN SNAPSHOT" in source
    assert "apiFetch(" not in source
    assert "fetch(" not in source
    assert "tryLive" not in source
    assert 'S.selectedEvent = "";' in source
    assert "详情区已清空" in source


def test_rule_gates_are_not_presented_as_model_confidence() -> None:
    source = terminal_source()
    assert "DETERMINISTIC_EVIDENCE_GATE" in source
    assert "未调用 · 证据门" in source
    assert "confidenceApplicable" in source
    assert "本次判断由谁完成" in source
    assert "影子模型分流" in source


def test_public_terminal_explains_workflow_without_fake_disabled_actions() -> None:
    source = terminal_source()
    assert "公开页面没有写入按钮" in source
    assert '<button class="btn primary" disabled' not in source
    assert "Evidence Agent 由后台在证据准备好后自动运行" in source
    assert "人工复核记录" in source


def test_public_terminal_uses_chinese_navigation_and_evidence_driven_next_actions() -> None:
    source = terminal_source()
    for label in ("态势总览", "事件工作台", "证据回放", "运行与模型", "双人盲审"):
        assert f">{label}</button>" in source
    assert 'class="en"' not in source
    assert "function nextActionGuidance(ev)" in source
    for code in (
        "EVIDENCE_CONFLICT",
        "MISSING_EVIDENCE",
        "WEAK_EVIDENCE",
        "HUMAN_RISK_REVIEW",
        "VERIFIED_MONITOR",
    ):
        assert code in source
    assert "这只是排序信号，不是交易方向" in source
    assert "不构成交易建议 · 不触发下单" in source


def test_backup_counts_distinguish_history_from_retained_files() -> None:
    source = terminal_source()
    assert "历史备份运行" in source
    assert "当前保留文件" in source
    assert "backups: { history_runs" in source
    assert "retained_daily_files" in source
    assert "retained_weekly_files" in source


def test_model_and_human_evaluation_pages_explain_current_meaning() -> None:
    source = terminal_source()
    assert 'id="model-status-banner"' in source
    assert "历史 V1 失败继续保存在评测档案中" in source
    assert "模型人工验收" in source
    assert "此页面不审核或改变实时事件" in source
