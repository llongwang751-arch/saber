"""Opt-in real-model research acceptance with an isolated local knowledge base.

Run: python scripts/smoke_research.py --live
Uses configured LLM credentials and makes bounded paid requests. No external
search or Docker is required. Test data remains under runtime/research-acceptance.
"""

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.ERROR)

from config.config import APIConfig, default_config
from internal.application.bootstrap import build_deps
from fastapi.testclient import TestClient



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Allow real model API calls')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this check uses the configured model provider')
    source = default_config()
    cfg = APIConfig()
    for name in ('llm_api_url', 'llm_api_key', 'llm_model'):
        setattr(cfg, name, getattr(source, name))
    cfg.temperature = 0.1
    cfg.research_max_rounds = 2
    cfg.research_max_llm_calls = 10
    # Reasoning providers may charge hidden reasoning tokens to completion usage.
    cfg.research_max_output_tokens = 10000
    cfg.research_timeout_seconds = 180
    cfg.skillhub_enabled = False
    cfg.chunk_size = 1000
    directory = ROOT / 'runtime' / 'research-acceptance' / uuid.uuid4().hex[:12]
    directory.mkdir(parents=True)
    os.environ['AGI_AUTH_REQUIRED'] = '0'
    os.environ['AGI_RUN_DB_PATH'] = str(directory / 'runs.db')
    os.environ['AGI_EVAL_DATABASE_URL'] = 'sqlite:///' + (directory / 'app.db').as_posix()
    os.environ['AGI_REQUIRED_DEPENDENCIES'] = ''
    deps = build_deps(cfg)
    with TestClient(deps.app) as client:
        material = '''# AGI-Saber 研究运行时验收资料
    计划审批：模型生成计划后，任务状态是 awaiting_plan_review，释放 worker。
    审批通过后才执行研究步骤；编辑计划增加版本号，过期版本的审批返回 HTTP 409。
    拒绝计划终止任务。研究员先检索文档、阅读原文，再分析缺口并继续搜索。
    来源账本通过文档 ID 和内容指纹去重，研究报告的引用指向实际取得的原始摘录。
    断点恢复：研究 checkpoint 保存来源、已完成步骤和预算使用情况。
    执行中断后，显式恢复创建关联到旧任务的新运行；旧任务的终态和事件保持不变。
    恢复会跳过已完成的步骤。代码派发结果不确定时，不会自动再次运行代码。
    沙箱不可用时仅生成代码，明确标注未执行，不在宿主机运行。
    '''
        upload = client.post('/api/upload', files={'file': ('runtime-evidence.md', material.encode(), 'text/markdown')})
        assert upload.is_success, upload.status_code
        request = client.post('/api/agent-runs', json={
            'message': '仅根据知识库的 AGI-Saber 研究运行时验收资料，用中文简要解释计划审批和断点恢复。只安排一个 research 步骤和一个 write 步骤，不联网不写代码；报告最多两节。',
            'mode': 'research', 'use_rag': True})
        assert request.is_success, request.status_code
        run_id = request.json()['run_id']
        endpoint = '/api/agent-runs/' + run_id
        deadline = time.monotonic() + 210
        reviewed = False
        while time.monotonic() < deadline:
            run = client.get(endpoint).json()
            if run['status'] == 'awaiting_plan_review' and not reviewed:
                plan = client.get(endpoint + '/plan').json()
                assert plan['plan']['steps']
                approval = client.post(endpoint + '/plan/review', json={'action': 'approve', 'version': plan['version']})
                assert approval.is_success, approval.status_code
                reviewed = True
                print('Live plan generated and approved.', flush=True)
            if run['status'] in {'completed', 'failed', 'interrupted', 'cancelled'}:
                break
            time.sleep(0.25)
        events = client.get(endpoint + '/events?limit=500').json()
        result = (run.get('result') or {}).get('response') or {}
        evidence = {'mode': 'real_llm_local_knowledge', 'run_id': run_id, 'status': run['status'],
                    'research_status': result.get('status'), 'reviewed': reviewed,
                    'sources': len(result.get('sources') or []), 'references': len(result.get('references') or []),
                    'usage': result.get('usage'), 'limitations': result.get('limitations'),
                    'events': [e['type'] for e in events], 'output_dir': str(directory)}
        (directory / 'acceptance.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'report.md').write_text(result.get('answer') or '', encoding='utf-8')
        print(json.dumps(evidence, ensure_ascii=False), flush=True)
        assert reviewed and run['status'] == 'completed' and result.get('sources') and result.get('references'), 'Live research did not produce a cited report'
        assert result.get('status') == 'completed', 'Live research returned only partial evidence; inspect acceptance.json'


if __name__ == "__main__":
    main()
