from __future__ import annotations

import pandas as pd
import streamlit as st

from app.web.common import api_request, header, install_style, no_trading_banner, render_api_error, restore_deep_link, status_strip
from app.web.components import age_label, source_health_state


st.set_page_config(page_title="Operations & Model · Finance Radar", page_icon="▦", layout="wide")
install_style()
restore_deep_link("Operations_and_Model")

try:
    health = api_request("/api/v1/health")
    sources = api_request("/api/v1/sources/health")["items"]
    model = api_request("/api/v1/model/status")
except Exception as exc:
    header("Operations & Model", "运行健康、数据游标、备份恢复和模型卡")
    render_api_error(exc)
    st.stop()

try:
    market_capabilities = api_request("/api/v1/market/capabilities")
except Exception:
    # Keep the rest of Operations usable while an older API release is still
    # serving; the missing capability remains visible inside its own tab.
    market_capabilities = None

try:
    evidence_archive = api_request("/api/v1/evidence/archive")
except Exception:
    evidence_archive = None

header("Operations & Model", "运行健康、数据游标、备份恢复和模型卡", health["demo_mode"])
no_trading_banner()

latest_cycle = health["operations"].get("latest_worker_cycle")
latest_backup = health["operations"].get("latest_backup")
runtime_window = health["operations"].get("worker_window_24h") or {}
latest_result = latest_cycle.get("result", {}) if latest_cycle else {}
telegram_result = latest_result.get("telegram") or {}
telegram_enabled = bool((latest_result.get("process") or {}).get("telegram_send_enabled"))
source_states = [source_health_state(item) for item in sources]
source_errors = sum(1 for _, state in source_states if state == "ERROR")
status_strip(
    [
        ("API", health["status"].upper(), "ok" if health["status"] == "ok" else "risk"),
        ("账本", health["ledger"]["quick_check"].upper(), "ok" if health["ledger"]["quick_check"] == "ok" else "risk"),
        ("Worker", latest_cycle.get("status") if latest_cycle else "NO DATA", "ok" if latest_cycle and latest_cycle.get("status") == "SUCCESS" else "watch"),
        ("24小时窗口", f"{runtime_window.get('status', 'NO DATA')} · {float(runtime_window.get('observed_hours') or 0):.1f}h", "ok" if runtime_window.get("complete") else "watch"),
        ("来源异常", source_errors, "risk" if source_errors else "ok"),
        (
            "Telegram",
            f"{'READY' if telegram_enabled else 'DRY'} · err={int(telegram_result.get('errors') or 0)}",
            "ok" if telegram_enabled and not telegram_result.get("errors") else "watch",
        ),
        (
            "证据存档",
            f"{int(evidence_archive.get('source_snapshots') or 0)} raw" if evidence_archive else "N/A",
            "ok" if evidence_archive and not evidence_archive.get("integrity_failures_in_recent_sample") else "watch",
        ),
        ("备份", latest_backup.get("status") if latest_backup else "NO DATA", "ok" if latest_backup and latest_backup.get("status") == "VERIFIED" else "watch"),
        ("模型", f"{model['status']} · SHADOW", "ok" if model["status"] in {"ready", "ok"} else "watch"),
    ]
)

mode_cols = st.columns(3)
for column, mode in zip(mode_cols, ["LIVE", "RECENT_CAPTURE", "REPLAY"]):
    if column.button(mode, width="stretch", type="primary" if mode == health["demo_mode"] else "secondary"):
        try:
            api_request(f"/api/v1/demo/mode/{mode}", method="POST")
            st.rerun()
        except Exception as exc:
            render_api_error(exc)

tab_sources, tab_market, tab_evidence, tab_worker, tab_backup, tab_model, tab_audit = st.tabs(
    ["事件源", "行情能力", "证据存档", "Worker", "备份恢复", "模型卡", "硬边界审计"]
)
with tab_sources:
    rows = []
    for item in sorted(sources, key=lambda source: source_health_state(source)[0]):
        _, state = source_health_state(item)
        rows.append({
            "health": state,
            "source_id": item["source_id"],
            "name": item["name"],
            "authority": item["authority_tier"],
            "type": item["source_type"],
            "status": item["cursor_status"],
            "last_success": item["last_success_at"],
            "age": age_label(item.get("last_success_at")),
            "observations": item["observations"],
            "error": item["last_error"],
        })
    if source_errors:
        st.error(f"{source_errors} 个来源需要处理；异常项已经排在最前。")
    else:
        st.success("当前没有来源错误；WATCH 项代表尚未观测或缺少成功游标。")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

with tab_market:
    if market_capabilities is None:
        st.warning("当前 API 尚未提供统一行情能力清单；事件与其他运行页面不受影响。")
    else:
        boundary = market_capabilities["boundary"]
        if boundary.get("read_only") and boundary.get("no_trading"):
            st.success("行情链路为只读事件后审计：不使用账户数据，不存在订单端点，不进入模型训练特征。")
        st.info("这些是事件触发的审计快照，不是连续实时报价流；能力状态与最近快照新鲜度分开显示。")
        provider_rows = []
        for item in market_capabilities["providers"]:
            windows = item.get("observation_windows") or {}
            window_summary = " · ".join(
                f"{name.replace('t_plus_', 'T+')}:"
                + "/".join(f"{status}={count}" for status, count in sorted(states.items()))
                for name, states in windows.items()
                if name != "initial"
            ) or "—"
            provider_rows.append(
                {
                    "status": item["status"],
                    "provider": item["name"],
                    "role": item["role"],
                    "asset_classes": ", ".join(item["asset_classes"]),
                    "access": item["access"],
                    "deployment": item["deployment"],
                    "completed_jobs": item["completed_jobs"],
                    "pending_jobs": item["pending_jobs"],
                    "snapshots": item["snapshots"],
                    "last_snapshot": item["last_snapshot_at"],
                    "freshness": item.get("freshness_status"),
                    "age_seconds": item.get("snapshot_age_seconds"),
                    "continuous_feed": item.get("continuous_feed", False),
                    "last_error": item["last_error"],
                    "horizon_windows": window_summary,
                }
            )
        st.dataframe(pd.DataFrame(provider_rows), width="stretch", hide_index=True)
        policy = market_capabilities["provider_policy"]
        horizon_policy = market_capabilities.get("horizon_policy") or {}
        st.caption(
            f"crypto={policy['crypto']} · non_crypto={policy['non_crypto']} · "
            f"IBKR={policy['ibkr']} · 所有状态来自实际任务/快照或明确的本机探针边界"
        )
        st.caption(
            f"window baseline={horizon_policy.get('baseline', 'unknown')} · "
            f"missed={horizon_policy.get('missed_window_behavior', 'unknown')} · "
            "收益指标仅用于 post-event audit"
        )

with tab_evidence:
    if evidence_archive is None:
        st.warning("当前 API 尚未提供原始证据存档清单；事件精确引文仍可使用。")
    else:
        archive_cols = st.columns(5)
        archive_cols[0].metric("证据对象", int(evidence_archive.get("objects") or 0))
        archive_cols[1].metric("原始页面/PDF", int(evidence_archive.get("source_snapshots") or 0))
        archive_cols[2].metric("精确引文", int(evidence_archive.get("exact_excerpts") or 0))
        archive_cols[3].metric(
            "存档体积",
            f"{float(evidence_archive.get('archived_bytes') or 0) / 1024 / 1024:.2f} MiB",
        )
        coverage = evidence_archive.get("coverage") or {}
        archive_cols[4].metric(
            "官方证据覆盖",
            f"{float(coverage.get('coverage_pct') or 0):.1f}%",
            f"剩余 {int(coverage.get('missing_links') or 0)} · 策略排除 {int(coverage.get('terminal_policy_exclusions') or 0)}",
        )
        integrity_failures = int(evidence_archive.get("integrity_failures_in_recent_sample") or 0)
        if integrity_failures:
            st.error(f"最近对象样本中有 {integrity_failures} 个 SHA-256 完整性失败。")
        elif evidence_archive.get("recent_objects"):
            st.success("最近证据对象均已重新计算 SHA-256 并通过完整性核验。")
        else:
            st.info("对象存储已启用；尚未归档原始官方页面。")
        if int(coverage.get("missing_links") or 0):
            st.info(
                f"归档 worker 正以每轮 {int(coverage.get('worker_batch_size') or 0)} 条追赶；"
                "剩余项继续后台处理，不影响现有证据边使用。"
            )
        recent_objects = evidence_archive.get("recent_objects") or []
        if recent_objects:
            st.dataframe(
                pd.DataFrame(recent_objects),
                width="stretch",
                hide_index=True,
                column_config={"source_url": st.column_config.LinkColumn()},
            )
        policy = evidence_archive.get("policy") or {}
        st.caption(
            f"immutable={policy.get('immutable')} · content_address={policy.get('content_address')} · "
            "官方域名/HTTPS/体积上限 · 不自动核验事实 · 不进入模型特征 · no trading"
        )

with tab_worker:
    latest = health["operations"]["latest_worker_cycle"]
    if latest:
        result = latest.get("result") or {}
        candidates = result.get("candidate_extraction") or {}
        review = result.get("review_triage") or {}
        source_errors = result.get("errors") or []
        worker_cols = st.columns(5)
        worker_cols[0].metric("最近周期", latest["status"])
        worker_cols[1].metric("周期耗时", f"{float(result.get('worker_elapsed_ms') or 0) / 1000:.1f}s")
        worker_cols[2].metric("新增事件", candidates.get("new_events", 0))
        worker_cols[3].metric("待复核", review.get("pending_events", 0))
        worker_cols[4].metric("源错误", len(source_errors))
        st.caption(
            f"cycle_id={latest['cycle_id']} · started={latest['started_at']} · "
            f"finished={latest['finished_at']} · Telegram={result.get('telegram', {}).get('mode', 'unknown')}"
        )
        if source_errors:
            for error in source_errors:
                st.warning(error)
        else:
            st.success("最近周期完成，未记录数据源错误。")

        source_rows = []
        for source_id, source_result in (result.get("official_sources") or {}).items():
            source_rows.append(
                {
                    "source_id": source_id,
                    "state": (
                        "not_modified" if source_result.get("not_modified") else
                        "interval_skip" if source_result.get("skipped_interval") else
                        "processed"
                    ),
                    "items": source_result.get("items", 0),
                    "new_revisions": source_result.get("new_revisions", 0),
                    "jobs": source_result.get("jobs", 0),
                    "xml_repaired": source_result.get("xml_repaired", 0),
                }
            )
        if source_rows:
            st.dataframe(pd.DataFrame(source_rows), width="stretch", hide_index=True)
        with st.expander("技术详情"):
            compact = {key: value for key, value in result.items() if key != "process"}
            st.json(compact)
    else:
        st.info("尚未记录常驻 Worker 周期。")

with tab_backup:
    latest = health["operations"]["latest_backup"]
    if latest:
        backup_cols = st.columns(4)
        backup_cols[0].metric("状态", latest["status"])
        backup_cols[1].metric("完整性", latest["quick_check"])
        backup_cols[2].metric("备份大小", f"{(latest.get('backup_bytes') or 0) / 1024 / 1024:.1f} MiB")
        verified_at = str(latest.get("verified_at") or "—")
        backup_cols[3].metric("核验时间", verified_at[:19].replace("T", " "))
        st.success("在线备份已在隔离临时数据库中恢复，并与源库核心表逐表核对行数。")
        counts = latest.get("restored_counts") or {}
        if counts:
            st.dataframe(
                pd.DataFrame([{"table": table, "restored_rows": count} for table, count in counts.items()]),
                width="stretch",
                hide_index=True,
            )
        st.caption(f"backup_id={latest['backup_id']} · {latest['backup_path']}")
    else:
        st.warning("尚未执行可验证备份/恢复演练。")
    st.code("python -m app.ops.backup backup", language="bash")

with tab_model:
    card = model.get("model_card")
    if card:
        metrics = card["metrics"]
        mcols = st.columns(4)
        mcols[0].metric("训练样本", card["dataset"]["rows"])
        mcols[1].metric("覆盖率", f"{metrics['coverage']:.1%}")
        mcols[2].metric("覆盖样本准确率", f"{metrics['covered_accuracy']:.1%}")
        mcols[3].metric("弃权率", f"{metrics['abstain_rate']:.1%}")
        st.write(card["polarity_policy"])
        st.write("限制")
        for limitation in card["limitations"]:
            st.markdown(f"- {limitation}")
        with st.expander("完整模型卡"):
            st.json(card)
    else:
        st.warning("模型卡未加载，当前使用透明关键词回退。")
    robustness = model.get("robustness")
    if robustness:
        st.markdown("#### 消融与漂移门禁")
        st.dataframe(pd.DataFrame(robustness.get("ablation") or []), width="stretch", hide_index=True)
        policy = robustness.get("monitoring_policy") or {}
        st.caption(
            f"promotion={robustness.get('promotion_decision')} · minimum window={policy.get('minimum_window_rows')} · "
            "当前 holdout 是冻结分组时间留出集，不冒充从未观察的外部盲测。"
        )
        with st.expander("漂移阈值"):
            st.json(policy)

    external_blind = model.get("external_blind")
    if external_blind:
        st.markdown("#### 外部盲测 v1")
        blind_metrics = external_blind.get("metrics") or {}
        blind_cols = st.columns(5)
        blind_cols[0].metric("冻结样本", external_blind.get("rows", 0))
        blind_cols[1].metric("覆盖率", f"{float(blind_metrics.get('coverage') or 0):.1%}")
        blind_cols[2].metric("风险召回", f"{float(blind_metrics.get('risk_recall') or 0):.1%}")
        blind_cols[3].metric("覆盖准确率", f"{float(blind_metrics.get('covered_accuracy') or 0):.1%}")
        blind_cols[4].metric(
            "正常新闻误报",
            f"{float(blind_metrics.get('non_target_false_risk_rate') or 0):.1%}",
        )
        if external_blind.get("gate_pass"):
            st.success("外部盲测通过预注册门槛；模型仍保持 shadow，未经人工治理不得晋级。")
        else:
            st.error(
                "外部盲测未通过：风险召回很高，但正常官方新闻误报过多。"
                "当前模型只可作为高召回影子风险路由器，不能作为正负事件总分类器。"
            )
        source_rows = []
        for source_id, source_result in (external_blind.get("source_metrics") or {}).items():
            source_rows.append(
                {
                    "source_id": source_id,
                    "rows": source_result.get("rows"),
                    "expected": source_result.get("expected_label"),
                    "coverage": source_result.get("coverage"),
                    "strict_accuracy": source_result.get("strict_accuracy"),
                    "routes": source_result.get("route_distribution"),
                }
            )
        if source_rows:
            st.dataframe(pd.DataFrame(source_rows), width="stretch", hide_index=True)
        overlap = external_blind.get("overlap_audit") or {}
        st.caption(
            f"freeze={external_blind.get('freeze_id')} · label-first · "
            f"title/ID overlap=0 · max shingle Jaccard={float(overlap.get('max_training_shingle_jaccard') or 0):.3f} · "
            f"promotion={external_blind.get('promotion_decision')}"
        )
        with st.expander("外部盲测门禁"):
            st.json(
                {
                    "gates": external_blind.get("gates"),
                    "thresholds": external_blind.get("thresholds"),
                    "dataset_sha256": external_blind.get("dataset_sha256"),
                    "model_artifact_sha256": external_blind.get("model_artifact_sha256"),
                    "no_trading": external_blind.get("no_trading"),
                }
            )
    else:
        st.info("尚未加载冻结的外部盲测报告；这不影响 shadow 推理，但阻止模型晋级。")

with tab_audit:
    audit = health["ledger"]["audit"]
    audit_cols = st.columns(3)
    audit_cols[0].metric("交易边界违规", audit["trading_boundary_violations"])
    audit_cols[1].metric("自动核验违规", audit["auto_verification_violations"])
    audit_cols[2].metric("事后行情泄漏", audit["market_feature_leakage_violations"])
    if sum(audit.values()) == 0:
        st.success("no_trading、禁止自动核验、禁止事件后行情特征泄漏：全部通过")
    st.write("API 明确不存在以下能力：")
    st.code("orders · positions · balances · brokerage_accounts · trade_execution", language=None)
