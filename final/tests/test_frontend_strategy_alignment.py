from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parents[1] / "web" / "src"


def _strategy_frontend_source() -> str:
    paths = [
        WEB_ROOT / "stores" / "evaluation.js",
        WEB_ROOT / "components" / "EvaluationDashboard.vue",
        WEB_ROOT / "components" / "StrategyReview.vue",
    ]
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in paths
        if path.is_file()
    )


def test_strategy_page_calls_review_activation_and_idempotent_rollback_apis():
    source = _strategy_frontend_source()

    assert "/api/eval/strategies" in source
    assert "/api/eval/promotion-proposals" in source
    assert "/decision" in source
    assert "/activate" in source
    assert "/strategies/rollback" in source
    assert "idempotency_key" in source


def test_strategy_page_never_presents_offline_evidence_as_online_ab():
    source = _strategy_frontend_source()

    assert "offline_eval" in source
    assert "离线策略" in source
    assert "不代表线上 A/B" in source
    assert "auto_activate" in source
    assert "显式激活" in source
    assert "统计证据不足" in source or "证据不足" in source
