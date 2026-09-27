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

# 把项目根（final/）加入 sys.path，让 `config.config` / `internal.*` 可被绝对导入
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Prefer the complete Vue build; keep the legacy single-file UI as fallback.
vue_dist = os.path.join(PROJECT_ROOT, "web", "dist")
legacy_frontend = os.path.join(PROJECT_ROOT, "frontend")
os.environ.setdefault("FRONTEND_DIR", vue_dist if os.path.isdir(vue_dist) else legacy_frontend)

from internal.application.bootstrap import Deps as Deps, build_deps as build_deps  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
                deps.close()
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
