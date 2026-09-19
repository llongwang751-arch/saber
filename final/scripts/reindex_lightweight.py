"""Explicit, resumable lightweight projection rebuild for existing SQLite users.

Uses configured application database and model credentials; never reads passwords
or prints source text. Run during maintenance, or use tenant-scoped HTTP reindex.
"""
from pathlib import Path
import argparse
import json
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from sqlalchemy import text
    from config.config import default_config
    from internal.application.store import ApplicationStore
    from internal.application.local_repos import LocalRagChunkRepo
    from internal.llm.llm import Client
    from internal.rag.rag import Engine

    cfg = default_config()
    if not cfg.rag_lightweight_enabled or not cfg.is_real_embedding() or not cfg.is_real_llm():
        raise RuntimeError('Enable lightweight mode and real embedding/chat credentials first')
    store = ApplicationStore()
    results = []
    try:
        with store.transaction() as session:
            users = list(session.execute(text('SELECT DISTINCT user_id FROM agent_rag_chunks')).scalars())
        repo = LocalRagChunkRepo(store)
        inf = SimpleNamespace(repo=SimpleNamespace(ragchunk=repo))
        client = Client(cfg)
        client._timeout = 45
        for user in users:
            engine = Engine(cfg, inf, client, user_id=user)
            result = engine.rebuild_indexes()
            if not result['complete']:
                raise RuntimeError('Incomplete projection rebuild')
            trace = {}
            engine._hybrid.search('Harness', 3, trace)
            result['probe_paths'] = trace.get('lightweight_paths')
            results.append(result)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(results))
    finally:
        store.close()


if __name__ == '__main__':
    main()
