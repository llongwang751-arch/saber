"""Read-only corpus snapshot -> isolated SQLite -> production RAG + real model.

This baseline measures the local retrieval path, not Milvus/ES/graph quality.
Gold anchors describe the stored README's claims, not independently verified facts.
No source-document commands are executed. No thresholds are tuned on this set.
"""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
import uuid
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Question, literal source anchor. Related questions share a group; no random split.
GROUPS = [
    ("identity", "Agent 执行层", ["tx-harness 是做什么的？", "README 中项目主要评测哪个层次？"]),
    ("python", "Python 3.10+", ["README 标注的 Python 版本要求是什么？", "资料里的 Python 版本徽章写了什么？"]),
    ("failure", "Error: failed", ["报错截断成什么内容会导致模型瞎重试？", "文档举出的错误信息截断问题是什么？"]),
    ("paths", "产物路径解析漂移", ["什么问题会让产物写错目录？", "产物路径解析漂移会造成什么后果？"]),
    ("compression", "上下文压缩把开头约束压没了", ["上下文压缩可能丢掉什么？", "资料指出上下文压缩有什么失败风险？"]),
    ("plugins", "Capability Seams", ["可单独替换的插件在资料中叫什么？", "Capability Seams 包含哪些可替换部分？"]),
    ("controls", "一次只动一个地方", ["控制变量的核心要求是什么？", "为什么评测时不能顺手改多个地方？"]),
    ("environment", "docs/environment.md", ["环境清单保存在哪个文件？", "锁环境阶段的环境清单路径是什么？"]),
    ("dimensions", "信息回传、产物落点、失败恢复、上下文管理、停止条件、权限边界、可观测性", ["Harness 能力拆解表列出了哪七个维度？", "能力拆解中的权限边界和可观测性以外还有哪些维度？"]),
    ("tasks", "自动客观外部检查脚本", ["每道评测题需要包含哪些材料？", "评测题库有没有外部客观检查脚本？"]),
    ("matrix", "results/matrix.json", ["批量跑的结果大表存在哪里？", "results 目录中的结果矩阵文件叫什么？"]),
    ("reliability", "scripts/evaluate.py", ["运行标尺可靠性自检的命令是什么？", "阶段 4 的自检脚本路径是什么？"]),
    ("batch", "--config all", ["怎样运行全部基准与七组对照实验？", "batch_run.py 运行完整矩阵需要什么参数？"]),
    ("attribution", "scripts/run_attribution.py", ["执行归因分析用哪个脚本？", "排查最弱零件的归因分析命令是什么？"]),
    ("remediation", "scripts/verify_remediation.py", ["运行定向修复闭环验证的命令是什么？", "验证修复前后通过率变化用哪个脚本？"]),
    ("unseen", "--dataset unseen", ["如何运行全部未知 BadCase 诊断？", "diagnose_badcase.py 指定未知案例集的参数是什么？"]),
    ("dashboard", "web/index.html", ["如何离线打开可视化看板？", "离线看板的 HTML 路径是什么？"]),
    ("evidence", "置信区间含 0", ["置信区间含 0 时应该给什么证据级别？", "Level 3 仅观察对应什么证据情况？"]),
]
UNANSWERABLE = [
    "tx-harness 的作者姓名是什么？", "tx-harness 采用哪种开源许可证？",
    "跑完整个评测总共花了多少人民币？", "评测模型精确版本号是什么？",
    "该项目部署的 PostgreSQL 端口是多少？", "该项目的 Neo4j 登录密码是什么？",
    "Web 看板默认监听哪个端口？", "每道任务的 token 预算上限是多少？",
    "压缩触发阈值具体是多少个 token？", "Bootstrap 重采样执行多少次？",
    "沙箱容器限制了多少 GB 内存？", "三层评分公式中的 C 精确定义和取值范围是什么？",
    "哪个提交修复了路径漂移，请给出 commit hash？", "新版和旧版 README 的版本冲突应该以哪一版为准？",
]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def snapshot(source):
    with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute("SELECT * FROM agent_rag_chunks ORDER BY id")]
    # Digest the logical corpus, unaffected by unrelated application database writes.
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return rows, digest


def cases_for(rows):
    cases = []
    for group, anchor, questions in GROUPS:
        relevant = [r["id"] for r in rows if anchor in (r["parent_content"] or r["content"])]
        if not relevant:
            raise ValueError("Source anchor absent: " + group)
        for question in questions:
            cases.append(dict(id=f"q{len(cases)+1:02}", group=group, question=question,
                              answerable=True, gold_anchor=anchor, source_ids=relevant))
    for question in UNANSWERABLE:
        cases.append(dict(id=f"q{len(cases)+1:02}", group="unanswerable", question=question,
                          answerable=False, gold_anchor=None, source_ids=[]))
    return cases


def run(args):
    os.environ["AGI_LLM_ALLOW_MOCK"] = "0"
    logging.disable(logging.CRITICAL)
    from config.config import default_config
    from internal.application.store import ApplicationStore
    from internal.application.local_repos import LocalRagChunkRepo
    from internal.llm.llm import Client, Message
    from internal.rag.rag import Engine

    rows, digest = snapshot(args.source)
    if len({r["user_id"] for r in rows}) != 1:
        raise ValueError("Select a single tenant corpus before evaluating; never merge tenants")
    cases = cases_for(rows)
    if getattr(args, 'resume_index', False):
        if args.mode != 'lightweight' or (args.output / 'results.json').exists():
            raise ValueError('Only an unfinished lightweight indexing run can be resumed')
        if json.loads((args.output / 'corpus.json').read_text(encoding='utf-8')) != rows:
            raise ValueError('Source corpus changed; start a new evaluation')
    else:
        args.output.mkdir(parents=True, exist_ok=False)  # Never overwrite a prior run.
    dump(args.output / "corpus.json", rows)
    dump(args.output / "cases.json", cases)
    cfg = default_config()
    cfg.rerank_api_url = ""
    cfg.rerank_api_key = ""
    cfg.rag_require_citations = True
    cfg.rag_lightweight_enabled = args.mode == 'lightweight'
    cfg.rag_lightweight_path = str((args.output / 'indexes').resolve())
    if not cfg.is_real_llm():
        raise RuntimeError("A real model is required")
    usage_records = []
    client = Client(cfg, usage_callback=usage_records.append)
    client._timeout = 45
    resources = []
    if args.mode in {"local", "lightweight"}:
        store = ApplicationStore("sqlite:///" + (args.output / "isolated.db").resolve().as_posix())
        resources.append(store)
        user = (store.find_user_by_username('kb-eval') if getattr(args, 'resume_index', False)
                else store.create_user("kb-eval", "disabled-login"))["id"]
        repo = LocalRagChunkRepo(store)
    else:
        from internal.platform.postgres import PostgresClient
        from internal.platform.es import ESClient
        from internal.platform.milvus import MilvusClientWrapper
        from internal.repo.ragchunk import Store
        cfg.pg_host, cfg.pg_port = "127.0.0.1", 15432
        cfg.pg_user, cfg.pg_password, cfg.pg_database = "eval", "eval-local-only", "agisaber_eval"
        cfg.es_addresses, cfg.es_username, cfg.es_password = ["http://127.0.0.1:19200"], "", ""
        cfg.milvus_host, cfg.milvus_port = "127.0.0.1", 19540
        pg = PostgresClient(cfg)
        resources.append(pg)
        assert pg.is_real(), "Isolated PG is unavailable"
        es = ESClient(cfg) if args.mode in {"keyword", "hybrid"} else None
        mv = MilvusClientWrapper(cfg) if args.mode in {"semantic", "hybrid"} else None
        if es:
            resources.append(es)
            assert es.is_real(), "Isolated ES is unavailable"
        if mv:
            resources.append(mv)
            assert mv.is_real(), "Isolated Milvus is unavailable"
        repo = Store(pg, mv, es)
        if es:
            assert repo.ensure_es_index() is None
        if mv:
            assert repo.ensure_milvus_collection(cfg.rag_milvus_dim) is None
        user = "eval-" + uuid.uuid4().hex
    id_map = {}
    for row in rows:
        vector = client.embed(row["content"]) if args.mode in {"semantic", "hybrid"} else []
        new_id = repo.save_pg_with_parent(row["doc_hash"], row["chunk_idx"], row["content"],
            row["parent_content"], json.dumps(vector), user_id=user, document_id=row["document_id"],
            version_id=row["version_id"], section=row["section"])
        assert new_id > 0
        id_map[new_id] = row["id"]
        if args.mode in {"keyword", "hybrid"}:
            assert repo.index_es(new_id, row["content"], row["doc_hash"], row["chunk_idx"], user_id=user) is None
        if args.mode in {"semantic", "hybrid"}:
            assert repo.insert_milvus([new_id], [row["content"]], [vector], user_id=user) is None
    if args.mode in {"keyword", "hybrid"}:
        repo.es.client.indices.refresh(index="rag_chunks")
    if args.mode in {"semantic", "hybrid"}:
        repo.milvus.client.flush(collection_name="rag_chunks")
        repo.milvus.client.load_collection(collection_name="rag_chunks")
    inf = SimpleNamespace(repo=SimpleNamespace(ragchunk=repo),
        ready=SimpleNamespace(milvus="connected" if args.mode in {"semantic", "hybrid"} else "disabled",
                              elasticsearch="connected" if args.mode in {"keyword", "hybrid"} else "disabled"))
    engine = Engine(cfg, inf, client, user_id=user)
    index_report = engine._hybrid.rebuild_lightweight() if args.mode == 'lightweight' else None
    if index_report is not None:
        assert index_report['complete'], index_report
        dump(args.output / 'index-report.json', index_report)
    calls = 0
    def generate(system, message):
        nonlocal calls
        calls += 1
        return client._call_chat(system, [Message(role="user", content=message)])
    engine.set_generate_fn(generate)
    other = Engine(cfg, inf, client, user_id="absent-tenant")
    isolation = not other.loaded and not other._hybrid.search("Harness", 10)
    results = []
    try:
        for case in cases[:args.limit]:
            started = time.perf_counter()
            result = dict(case)
            try:
                answer, evidence, trace = engine.query_with_history_trace(case["question"])
                hits = trace.get("retrieval", {}).get("final_candidates", [])
                ranks = [rank for rank, hit in enumerate(hits, 1)
                         if id_map[hit["pg_id"]] in case["source_ids"]]
                result.update(answer=answer, trace=trace, error=None,
                    retrieval_hit=bool(ranks), reciprocal_rank=1/min(ranks) if ranks else 0,
                    answered=bool(evidence),
                    # This is only source provenance, not answer correctness/entailment.
                    cited_gold=any(id_map[e["pg_id"]] in case["source_ids"] and e.get("claims")
                                   for e in evidence))
            except Exception as exc:
                result.update(error=type(exc).__name__, answered=False, retrieval_hit=False,
                              reciprocal_rank=0, cited_gold=False)
            result["latency_ms"] = round(1000*(time.perf_counter()-started), 2)
            results.append(result)
            dump(args.output / "results.json", results)
            print(json.dumps({k: result[k] for k in ("id", "answered", "error", "latency_ms")}), flush=True)
    finally:
        for resource in reversed(resources):
            resource.close()
    positives = [r for r in results if r["answerable"]]
    negatives = [r for r in results if not r["answerable"]]
    def rate(items, predicate):
        return sum(bool(predicate(r)) for r in items)/len(items) if items else None
    summary = dict(profile=f"existing-kb-{args.mode}-real-generation", corpus_sha256=digest,
        index_report=index_report,
        resumed_index=bool(getattr(args, 'resume_index', False)),
        source_unchanged=snapshot(args.source)[1] == digest, chunk_count=len(rows),
        document_count=len({r["document_id"] for r in rows}), test_count=len(results),
        real_generation_calls=calls, tenant_isolation_passed=isolation,
        token_usage={key: sum(r.get(key, 0) for r in usage_records)
                     for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
        responses_with_usage=sum("total_tokens" in r for r in usage_records),
        refusal_reasons={reason: sum(r.get("trace", {}).get("reason") == reason for r in results)
                         for reason in sorted({r.get("trace", {}).get("reason", "") for r in results}) if reason},
        errors=sum(bool(r["error"]) for r in results),
        retrieval_hit_at_k=rate(positives, lambda r:r["retrieval_hit"]),
        mrr=sum(r["reciprocal_rank"] for r in positives)/len(positives) if positives else None,
        answerable_response_rate=rate(positives, lambda r:r["answered"] and not r["error"]),
        answerable_gold_citation_rate=rate(positives, lambda r:r["cited_gold"] and not r["error"]),
        unanswerable_refusal_rate=rate(negatives, lambda r:not r["answered"] and not r["error"]),
        latency_median_ms=statistics.median(r["latency_ms"] for r in results),
        latency_p95_ms=sorted(r["latency_ms"] for r in results)[max(0, __import__('math').ceil(.95*len(results))-1)],
        top_k=cfg.top_k, fallback_threshold=cfg.rag_fallback_min_overlap,
        limitations=["single document; no cross-document evaluation", f"retrieval mode: {args.mode}; graph enabled only in lightweight mode",
            "fixed baseline; no threshold tuning or held-out calibration",
            "citation provenance is not semantic entailment; manual answer review needed",
            "usage covers synchronous generation only; provider pricing not assumed",
            "usage is process-local; an earlier failed indexing attempt is not included after resume",
            "sequential warm-process run; no concurrency or recovery measurement"])
    dump(args.output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=True), flush=True)
    return 1 if summary["errors"] or not isolation or not summary["source_unchanged"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("runtime/evaluation.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=range(1,51), default=50)
    parser.add_argument("--mode", choices=["local", "keyword", "semantic", "hybrid", "lightweight"], default="local")
    parser.add_argument('--resume-index', action='store_true')
    raise SystemExit(run(parser.parse_args()))
