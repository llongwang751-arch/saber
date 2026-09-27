"""Read-only real dependency probes and grounded generation smoke test.

No production corpus writes. No replay or mock responses are accepted.
This is NOT a retrieval-quality, load, or disaster-recovery benchmark.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def run(output, profile='configured'):
    os.environ['AGI_LLM_ALLOW_MOCK'] = '0'
    from config.config import default_config
    from internal.llm.llm import Client, Message
    from internal.rag.rag import Engine
    cfg = default_config()
    if profile == 'isolated':
        cfg.pg_host, cfg.pg_port = '127.0.0.1', 15432
        cfg.pg_user, cfg.pg_password, cfg.pg_database = 'eval', 'eval-local-only', 'agisaber_eval'
        cfg.es_addresses = ['http://127.0.0.1:19200']
        cfg.es_username = cfg.es_password = ''
        cfg.neo4j_uri = 'bolt://127.0.0.1:17687'
        cfg.neo4j_user, cfg.neo4j_password = 'neo4j', 'eval-local-only'
        cfg.milvus_host, cfg.milvus_port = '127.0.0.1', 19540
    logging.disable(logging.CRITICAL)  # Provider errors can contain sensitive endpoint details.
    rows = []
    def check(name, fn):
        started = time.perf_counter()
        try:
            evidence = fn()
            row = {'name': name, 'passed': True, 'evidence': evidence}
        except Exception as exc:
            row = {'name': name, 'passed': False, 'error_type': type(exc).__name__}
        row['latency_ms'] = round((time.perf_counter() - started) * 1000, 2)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=True), flush=True)

    def pg():
        import psycopg2
        conn = psycopg2.connect(host=cfg.pg_host or 'localhost', port=cfg.pg_port,
            user=cfg.pg_user, password=cfg.pg_password, dbname=cfg.pg_database, connect_timeout=3)
        try:
            with conn.cursor() as cursor:
                cursor.execute('SELECT 1')
                assert cursor.fetchone()[0] == 1
            return {'read_only_query': 'SELECT 1'}
        finally:
            conn.close()

    def es():
        import requests
        if not cfg.es_addresses:
            raise RuntimeError('not configured')
        response = requests.get(cfg.es_addresses[0].rstrip('/') + '/_cluster/health',
            auth=(cfg.es_username, cfg.es_password) if cfg.es_username else None, timeout=3)
        response.raise_for_status()
        status = response.json()['status']
        if status == 'red':
            raise RuntimeError('cluster unhealthy')
        return {'cluster_status': status}

    def neo():
        from neo4j import GraphDatabase
        with GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_password),
                                  connection_timeout=3, connection_acquisition_timeout=3) as driver:
            with driver.session() as session:
                assert session.run('RETURN 1 AS ok').single()['ok'] == 1
        return {'read_only_query': 'RETURN 1'}

    def milvus():
        from pymilvus import MilvusClient
        client = MilvusClient(uri=f'http://{cfg.milvus_host}:{cfg.milvus_port}', timeout=3)
        try:
            return {'collection_count': len(client.list_collections(timeout=3))}
        finally:
            client.close()

    client = Client(cfg)
    client._timeout = 45
    def embedding():
        if not cfg.is_real_embedding():
            raise RuntimeError('embedding not configured')
        vector = client.embed('优化验证：告警送达时限为三十秒。')
        assert len(vector) > 0
        return {'dimensions': len(vector), 'configured_dimensions': cfg.rag_milvus_dim,
                'dimension_match': len(vector) == cfg.rag_milvus_dim}

    def generation():
        if not cfg.is_real_llm():
            raise RuntimeError('model not configured')
        # Exercise the real answer composer on a fixed source, independently of retrieval.
        engine = object.__new__(Engine)
        engine.cfg = cfg
        engine.set_generate_fn(lambda system, user: client._call_chat(system, [Message(role='user', content=user)]))
        answer, evidence = engine._compose_answer('严重告警必须在多少秒内送达？', [
            {'pg_id': 1, 'content': '测试资料规定：严重告警必须在 30 秒内送达。普通告警五分钟内送达。', 'source': 'keyword', 'score': .01}])
        assert evidence and '[E1]' in answer and '30' in answer
        return {'answer': answer, 'claims': evidence[0].get('claims', []),
                'validation': 'quote provenance only; not semantic entailment'}

    for name, fn in [('postgresql', pg), ('elasticsearch', es), ('neo4j', neo),
                     ('milvus', milvus), ('real_embedding', embedding), ('real_grounded_generation', generation)]:
        check(name, fn)
    report = {'profile': 'real-readonly-smoke', 'infrastructure_profile': profile, 'mock_allowed': False,
              'production_corpus_modified': False, 'release_evidence': False,
              'checks': rows, 'passed': all(row['passed'] for row in rows)}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('docs/live-optimization-smoke.json'))
    parser.add_argument('--profile', choices=['configured', 'isolated'], default='configured')
    args = parser.parse_args()
    sys.exit(run(args.output, args.profile))
