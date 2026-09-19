from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"


def _read(relative: str) -> str:
    return (WEB / relative).read_text(encoding="utf-8")


def test_online_experiment_control_plane_exposes_full_lifecycle_contract():
    store = _read("stores/experiments.js")

    assert "/api/online-experiments/readiness" in store
    assert "/api/online-experiments/deployments" in store
    assert "/api/online-experiments/experiments" in store
    assert "/analysis" in store
    assert "/audit" in store
    for operation in (
        "/submit",
        "/decision",
        "/start",
        "/ramp",
        "/pause",
        "/resume",
        "/complete",
        "/rollback",
    ):
        assert operation in store
    assert "expected_generation" in store
    assert "idempotency_key" in store


def test_online_experiment_is_a_top_level_truthful_workspace():
    dashboard = _read("components/EvaluationDashboard.vue")
    panel = _read("components/OnlineExperimentPanel.vue")

    assert "OnlineExperimentPanel" in dashboard
    assert "离线评测" in dashboard
    assert "真实在线实验" in dashboard
    for truth_label in (
        "未接生产流量",
        "内部演练",
        "正在收集真实曝光",
        "可下结论",
        "安全暂停",
        "已回滚",
    ):
        assert truth_label in panel
    assert "尚不能宣称效果" in panel
    assert "真实曝光" in panel
    assert "结果反馈（Outcome）覆盖" in panel
    assert "SRM" in panel
    assert "预注册校验和" in panel
    assert "每组所需样本 N" in panel
    assert "后台预置业务账号" in panel
    assert "非管理角色且早于实验提交建立" in panel
    assert "审计记录" in panel


def test_effect_statistics_are_rendered_only_after_all_claim_gates_pass():
    store = _read("stores/experiments.js")
    panel = _read("components/OnlineExperimentPanel.vue")

    assert "this.analysis?.has_real_traffic === true" in store
    assert "state.analysis?.sample?.sufficient === true" in store
    assert "state.analysis?.analysis_status === 'confirmatory'" in store
    assert "state.analysis?.truth === 'observed'" in store
    assert "state.analysis?.can_claim_effect === true" in store
    assert '<dl v-if="experiments.canAnalyze" class="conclusion-values">' in panel
    assert "无显著赢家" in panel
    assert "不得宣称线上提升" in panel
    assert "当前证据不满足真实流量、样本量、实验时长、分流比例异常检查（SRM）、安全门禁及完成状态" in panel


def test_online_lifecycle_has_role_state_confirmation_and_optimistic_locking():
    panel = _read("components/OnlineExperimentPanel.vue")
    store = _read("stores/experiments.js")

    assert "canManage" in panel
    assert "canApprove" in panel
    assert "无管理权限" in panel
    assert "无审批权限" in panel
    assert "window.confirm" in panel
    assert "rollbackConfirm !== selected.name" in panel
    assert "selected.status === 'draft'" in panel
    assert "selected.status === 'pending_review'" in panel
    assert "['canary','running']" in panel
    assert "expected_generation: Number(experiment?.generation" in store
    assert "idempotency_key: requestKey" in store


def test_chat_is_at_most_once_and_exposes_only_eligible_feedback_handle():
    chat = _read("stores/chat.js")
    bubble = _read("components/MessageBubble.vue")

    assert "const turnRequestId = requestId()" in chat
    assert chat.count("'X-Request-ID': turnRequestId") == 1
    assert "apiFetch('/api/chat'," not in chat
    assert "为避免重复调用工具，系统没有自动重试" in chat
    assert "if (!resp.ok)" in chat
    assert "const body = { message: msg, use_rag: this.ragOn, conversation_id: sessionId }" in chat
    assert "data.experiment?.feedback_eligible !== true" in chat
    assert "data.success === false" in chat
    assert "data.interrupted === true" in chat
    assert "/api/online-experiments/exposures/${encodeURIComponent(message.experimentExposureId)}/feedback" in chat
    assert "JSON.stringify({ rating, event_id: eventId })" in chat
    assert "msg.feedbackEligible === true" in bubble
    assert "msg.experimentExposureId && !msg.streaming && !msg.interrupted" in bubble
    assert "有帮助" in bubble
    assert "需要改进" in bubble
    assert ".arm" not in chat
    assert "arm" not in bubble.lower()
