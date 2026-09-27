"""Start the research workbench on loopback with its own persistent SQLite data.

Reuses model/search credentials from the existing configuration, without starting
the legacy external databases. Register an account in the browser on first use.
"""

import argparse
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.config import APIConfig, default_config
from internal.application.bootstrap import build_deps


def build_local_config(*, sandbox=True):
    source = default_config()
    cfg = APIConfig()
    for name in ("llm_api_url", "llm_api_key", "llm_model", "llm_fast_model",
                 "search_api_key", "search_api_url"):
        setattr(cfg, name, getattr(source, name))
    cfg.temperature = 0.1
    cfg.chunk_size = 1000
    cfg.top_k = 5
    cfg.research_max_output_tokens = 16000
    cfg.skillhub_enabled = False
    cfg.sandbox_enabled = sandbox
    cfg.sandbox_backend = "docker"
    cfg.sandbox_image = "python:3.11-slim"
    cfg.sandbox_network_disabled = True
    cfg.sandbox_readonly_rootfs = True
    directory = ROOT / "runtime" / "research-local"
    directory.mkdir(parents=True, exist_ok=True)
    secret_file = directory / "jwt-secret"
    try:
        with secret_file.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_urlsafe(48))
        secret_file.chmod(0o600)
    except FileExistsError:
        pass
    cfg.auth_jwt_secret = secret_file.read_text(encoding="utf-8").strip()
    os.environ["AGI_AUTH_REQUIRED"] = "1"
    os.environ["AGI_EVAL_DATABASE_URL"] = "sqlite:///" + (directory / "app.db").as_posix()
    os.environ["AGI_RUN_DB_PATH"] = str(directory / "runs.db")
    os.environ["AGI_REQUIRED_DEPENDENCIES"] = ""
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-sandbox", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    os.chdir(ROOT)
    cfg = build_local_config(sandbox=not args.no_sandbox)
    if not all((cfg.llm_api_url, cfg.llm_api_key, cfg.llm_model)):
        parser.error("Configure AGI_LLM_API_URL, AGI_LLM_API_KEY and AGI_LLM_MODEL in .env first")
    if not (ROOT / "web" / "dist" / "index.html").is_file():
        parser.error("Build the frontend first: npm --prefix web run build")
    import uvicorn

    deps = build_deps(cfg)
    print(f"Research workbench: http://127.0.0.1:{args.port}", flush=True)
    print(f"Web search configured: {bool(cfg.search_api_key)}; account registration required", flush=True)
    try:
        uvicorn.run(deps.app, host="127.0.0.1", port=args.port)
    finally:
        deps.close()


if __name__ == "__main__":
    main()
