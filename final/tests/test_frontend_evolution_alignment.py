from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "src"


def _read(relative: str) -> str:
    return (WEB / relative).read_text(encoding="utf-8")


def test_controlled_evolution_store_uses_full_audited_api_contract():
    store = _read("stores/evaluation.js")

    assert "/api/eval/evolution-suggestions" in store
    assert "/decision" in store
    assert "/materialize" in store
    assert "/audit-events" in store
    assert "expected_generation" in store
    assert "idempotency_key" in store
    assert "evolution-create" in store
    assert "evolution-review" in store
    assert "evolution-materialize" in store


def test_controlled_evolution_panel_is_chinese_and_truthful():
    dashboard = _read("components/EvaluationDashboard.vue")
    panel = _read("components/EvolutionSuggestionPanel.vue")

    assert "EvolutionSuggestionPanel" in dashboard
    assert "受控策略建议" in dashboard
    for statement in (
        "它不会“自己学习并上线”，也不代表效果已经提升",
        "当前只能说：已生成待验证的参数假设",
        "合成或回放证据",
        "不调用真实模型、工具或 RAG",
        "不得当作真实智能体效果",
        "创建者不能审批自己的建议",
        "接受不等于策略已创建",
        "不会创建晋级候选、激活策略或部署线上流量",
        "已物化，但尚未证明有效",
    ):
        assert statement in panel
    assert "RAG top_k" in panel
    assert "无答案阈值" in panel


def test_controlled_evolution_ui_requires_explicit_human_actions_and_shows_evidence():
    panel = _read("components/EvolutionSuggestionPanel.vue")

    assert "window.confirm" in panel
    assert "selected.status === 'proposed'" in panel
    assert "selected.status === 'accepted'" in panel
    assert "!selected.materialized_strategy_version_id" in panel
    assert "evidence_checksum" in panel
    assert "manifest_checksum" in panel
    assert "rule_version" in panel
    assert "evidence_complete" in panel
    assert "no_auto_apply" in panel
    assert "审计记录" in panel


def test_controlled_evolution_panel_has_accessible_feedback_and_keyboard_surfaces():
    panel = _read("components/EvolutionSuggestionPanel.vue")

    assert 'role="dialog"' in panel
    assert 'aria-modal="true"' in panel
    assert '@keydown.esc' in panel
    assert 'role="alert"' in panel
    assert 'aria-live="polite"' in panel
    assert 'aria-label="受控策略建议真实性说明"' in panel
    assert ':disabled="busy"' in panel
    assert ":focus-visible" in panel
    assert "prefers-reduced-motion" in panel
    assert "min-height:44px" in panel
