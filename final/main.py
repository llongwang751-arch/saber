# Final Stage — 全阶段整合 AI 助手（Python 版）
#
# 启动入口：
# - 加载配置（路径与 cwd 解耦）
# - 初始化基础设施
# - 构建统一智能体
# - 注册 HTTP 路由
# - 启动 FastAPI 服务
import logging
import os
import sys
from dataclasses import dataclass

# 把项目根（final/）加入 sys.path，让 `config.config` / `internal.*` 可被绝对导入
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Prefer the complete Vue build; keep the legacy single-file UI as fallback.
vue_dist = os.path.join(PROJECT_ROOT, "web", "dist")
legacy_frontend = os.path.join(PROJECT_ROOT, "frontend")
os.environ.setdefault("FRONTEND_DIR", vue_dist if os.path.isdir(vue_dist) else legacy_frontend)

from config.config import default_config  # noqa: E402
from internal.agent.agent import UnifiedAgent  # noqa: E402
from internal.handler.handler import setup_routes  # noqa: E402
from internal.infra.infra import Infrastructure  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass
class Deps:
    cfg: object
    inf: Infrastructure
    agent: UnifiedAgent
    app: object


def build_deps():
    cfg = default_config()
    from internal.application.auth import KNOWN_DEVELOPMENT_SECRETS

    auth_required = os.environ.get("AGI_AUTH_REQUIRED", "1").strip().lower() not in {"0", "false", "no", "off"}
    jwt_secret = str(cfg.auth_jwt_secret or "")
    if auth_required and (
        len(jwt_secret.encode("utf-8")) < 32
        or jwt_secret in KNOWN_DEVELOPMENT_SECRETS
    ):
        raise RuntimeError(
            "JWT 密钥必须是不少于 32 字节的强随机值且不能是公开的开发密钥；"
            "请设置 AGI_JWT_SECRET 或 JWT_SECRET"
        )
    if cfg.pprof_enabled and not str(cfg.pprof_admin_token or "").strip():
        raise RuntimeError("诊断端点已启用但 PPROF_ADMIN_TOKEN 未配置，拒绝启动")
    inf = Infrastructure(cfg)
    agent = UnifiedAgent(cfg, inf)
    app = setup_routes(agent, inf, cfg, auth_required=auth_required)
    return Deps(cfg=cfg, inf=inf, agent=agent, app=app)


def main():
    deps = None
    try:
        deps = build_deps()
        print_banner(deps.cfg, deps.inf)

        import uvicorn

        port = int(os.environ.get("AGI_SERVER_PORT", deps.cfg.server_port))
        uvicorn.run(deps.app, host="0.0.0.0", port=port)
    finally:
        if deps is not None:
            try:
                deps.inf.close()
            except Exception:
                pass


def print_banner(cfg, inf):
    addr = f":{os.environ.get('AGI_SERVER_PORT', cfg.server_port)}"
    print("========================================")
    print("Final Stage · AGI 智能助手启动成功")
    print("========================================")
    print(f"[INFO] Service       http://localhost{addr}")
    print(f"[INFO] 通用模型           {cfg.llm_model}")
    print(f"[INFO] Embedding     {cfg.embedding_model}")
    print("----------------------------------------")
    print(f"[INFO] Milvus        {inf.ready.milvus}")
    print(f"[INFO] PostgreSQL    {cfg.pg_host}:{cfg.pg_port}")
    print(f"[INFO] ElasticSearch {inf.ready.elasticsearch}")
    print(f"[INFO] Kafka         {inf.ready.kafka}")
    print("----------------------------------------")
    print("[READY] 道阻且长，行则将至。")
    print("========================================")


if __name__ == "__main__":
    main()
