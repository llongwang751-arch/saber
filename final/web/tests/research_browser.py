"""Browser contract smoke test with mocked API; no live model/credentials required.

Start Vite on 5178, then python tests/research_browser.py (requires Playwright + Edge).
This verifies UI behavior, not backend or research quality.
"""
import argparse
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:5178')
    parser.add_argument('--output', default=str(Path(__file__).resolve().parents[2] / 'tmp/research-ui'))
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    state = {'runs': {}, 'events': {}, 'reviews': [], 'streams': [], 'conflict': False, 'creates': []}
    plan = {'objective': '比较 Agent 研究框架的执行与恢复机制', 'constraints': ['使用官方文档', '标明证据不足'], 'steps': [
        {'id': 'research-1', 'title': '核查官方架构', 'kind': 'research', 'guidance': '检索持久化与任务恢复的官方资料。', 'acceptance': '收集可追溯的来源', 'depends_on': [], 'tool_policy': ['web_search']},
        {'id': 'write-1', 'title': '汇总研究报告', 'kind': 'write', 'guidance': '引用来源，区分事实与推断。', 'acceptance': '生成带引用报告', 'depends_on': ['research-1'], 'tool_policy': []},
    ]}
    sources = [{'source_id': 'S1', 'url': 'https://example.org/research', 'url_or_doc_id': 'https://example.org/research', 'title': '架构资料', 'content': '任务通过检查点保存研究状态。', 'round': 1, 'query': 'agent durable research'},
               {'source_id': 'S2', 'url': 'javascript:alert(1)', 'title': '<img src=x onerror=alert(1)>', 'content': '此来源地址不得变成可执行链接。'}]

    def event(run_id, kind, data):
        records = state['events'].setdefault(run_id, [])
        records.append({'event_id': len(records) + 1, 'type': kind, 'data': data, 'created_at': 1770000000})

    def new_run(message='研究恢复任务'):
        run_id = f"run-{len(state['runs']) + 1}"
        run = {'run_id': run_id, 'kind': 'research', 'status': 'awaiting_plan_review', 'conversation_id': 'conversation-1', 'created_at': 1770000000, 'message': message, 'plan': copy.deepcopy(plan), 'plan_version': 1, 'plan_status': 'pending', 'sources': [], 'artifacts': [], 'result': {}}
        state['runs'][run_id] = run
        event(run_id, 'queued', {})
        event(run_id, 'plan_created', {'plan': run['plan'], 'version': 1, 'status': 'awaiting_plan_review'})
        event(run_id, 'plan_review_required', {'version': 1, 'status': 'awaiting_plan_review'})
        return run

    def complete(run):
        run_id = run['run_id']
        event(run_id, 'node_start', {'id': 'research-1'})
        event(run_id, 'research_round', {'step_id': 'research-1', 'round': 1, 'status': 'completed'})
        event(run_id, 'source_found', sources[0])
        event(run_id, 'node_done', {'id': 'research-1', 'status': 'completed'})
        event(run_id, 'token', {'content': '研究结论'})
        report = '# 研究结论\n检查点支持恢复 [1](#source-1)。\n<script>window.reportInjected = true</script>\n<img src=x onerror="window.reportInjected = true">'
        run.update(status='completed', sources=copy.deepcopy(sources), result={'response': {'status': 'partial', 'answer': report, 'report_markdown': report, 'sources': copy.deepcopy(sources), 'references': [{'number': 1, 'source_id': 'S1'}], 'evidence': [{'source_id': 'S1', 'quote': '任务通过检查点保存研究状态。', 'claim': '支持恢复'}], 'steps': [{'id': 'research-1', 'status': 'completed', 'rounds': 1}], 'limitations': ['示例数据仅验证界面流程'], 'artifacts': [{'name': 'research-report.md', 'media_type': 'text/markdown', 'content': report}]}})
        event(run_id, 'done', {})

    def handle(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if not path.startswith('/api/'):
            return route.continue_()
        def respond(data, status=200):
            route.fulfill(status=status, content_type='application/json', body=json.dumps(data, ensure_ascii=False))
        if path == '/api/status': return respond({'features': {'research': True, 'medical': False, 'farm': False, 'experiments': False}})
        if path == '/api/agent-runs/summary': return respond({'active': 0, 'capacity': 4})
        if path == '/api/agent-runs':
            if request.method == 'POST':
                body = request.post_data_json
                state['creates'].append(body)
                assert request.headers.get('idempotency-key')
                return respond(new_run(body['message']))
            return respond(list(reversed(list(state['runs'].values()))))
        if path.startswith('/api/agent-runs/'):
            parts = path.split('/')
            run_id = parts[3]
            run = state['runs'][run_id]
            suffix = '/'.join(parts[4:])
            if suffix == 'events':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                return respond([item for item in state['events'][run_id] if item['event_id'] > after])
            if suffix == 'stream':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                state['streams'].append(after)
                if run['status'] == 'pending': complete(run)
                # Deliberately replay the cursor once to exercise UI duplicate suppression.
                records = [item for item in state['events'][run_id] if item['event_id'] >= after]
                wire = ''.join(f"id: {item['event_id']}\nevent: {item['type']}\ndata: {json.dumps(item['data'], ensure_ascii=False)}\n\n" for item in records)
                return route.fulfill(status=200, content_type='text/event-stream', body=wire)
            if suffix == 'plan/review':
                body = request.post_data_json
                state['reviews'].append(body)
                if state['conflict']:
                    state['conflict'] = False
                    run['plan_version'] += 1
                    run['plan']['objective'] = '其他页面更新后的研究目标'
                    return respond({'detail': 'plan version conflict'}, 409)
                assert body['version'] == run['plan_version']
                if body['action'] == 'edit':
                    run.update(plan=body['plan'], plan_version=run['plan_version'] + 1)
                    event(run_id, 'plan_edited', {'plan': run['plan'], 'version': run['plan_version'], 'status': run['status']})
                elif body['action'] == 'approve':
                    run.update(status='pending', plan_status='approved')
                    event(run_id, 'plan_approved', {'version': run['plan_version'], 'status': 'pending'})
                else:
                    run.update(status='cancelled', plan_status='rejected')
                    event(run_id, 'plan_rejected', {'version': run['plan_version'], 'status': 'cancelled'})
                return respond({'run_id': run_id, 'status': run['status'], 'plan': run['plan'], 'version': run['plan_version'], 'review_status': run['plan_status']})
            if suffix == 'cancel':
                run['status'] = 'cancelled'
                event(run_id, 'cancel_requested', {})
                event(run_id, 'done', {})
                return respond(run)
            if suffix == 'resume':
                continued = new_run(run['message'])
                continued.update(status='pending', plan_status='approved', parent_run_id=run_id)
                return respond(continued)
            return respond(run)
        if path in ['/api/skills/installed', '/api/tool-approvals']: return respond([])
        return respond({'items': [], 'documents': [], 'skills': []})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel='msedge')
        context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce', accept_downloads=True)
        context.add_init_script("localStorage.setItem('agi_auth_token', 'ui-test-token'); localStorage.setItem('agi_auth_user', 'UI Test');")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('**/api/**', handle)
        page.goto(args.base_url, wait_until='networkidle')
        expect(page.get_by_role('button', name='智慧云诊室', exact=True)).to_have_count(0)
        page.get_by_role('button', name='研究工作台', exact=True).click()
        dialog = page.get_by_role('dialog', name='Saber 运行记录')
        expect(dialog).to_be_visible()
        page.locator('#native-run-message').fill('比较 Agent 框架的研究与恢复机制')
        page.get_by_role('button', name='生成研究计划', exact=True).click()
        expect(page.get_by_role('heading', name='审核研究计划')).to_be_visible()
        assert state['creates'][-1]['mode'] == 'research'
        page.get_by_role('button', name='修改计划', exact=True).click()
        page.get_by_label('研究目标', exact=True).fill('用户修改后的研究目标')
        page.wait_for_timeout(4200)
        expect(page.get_by_label('研究目标', exact=True)).to_have_value('用户修改后的研究目标')
        page.get_by_role('button', name='保存新版本', exact=True).click()
        expect(page.locator('.section-kicker', has_text='PLAN / V2')).to_be_visible()
        state['conflict'] = True
        page.get_by_role('button', name='批准并开始研究', exact=True).click()
        expect(page.get_by_role('alert').filter(has_text='计划已在其他页面更新')).to_be_visible()
        expect(page.locator('.section-kicker', has_text='PLAN / V3')).to_be_visible()
        expect(page.locator('.plan-objective')).to_have_text('其他页面更新后的研究目标')
        page.screenshot(path=str(output / 'plan-review-desktop.png'), full_page=True)
        page.get_by_role('button', name='批准并开始研究', exact=True).click()
        expect(page.get_by_role('heading', name='研究报告', exact=True)).to_be_visible()
        expect(page.locator('.run-meta strong')).to_have_text('已完成')
        assert [review['version'] for review in state['reviews']] == [1, 2, 3]
        expect(page.locator('.report-body script, .report-body img')).to_have_count(0)
        assert page.evaluate('window.reportInjected') is None
        expect(page.locator('a[href^="javascript:"]')).to_have_count(0)
        page.get_by_role('link', name='查看来源 S1').click()
        expect(page.locator('#research-source-S1')).to_be_focused()
        with page.expect_download() as downloaded:
            page.get_by_role('button', name='下载 Markdown', exact=True).click()
        assert downloaded.value.suggested_filename == 'research-report.md'
        page.locator('.research-report').evaluate('(element) => element.scrollIntoView({ block: "start" })')
        page.screenshot(path=str(output / 'report-desktop.png'), full_page=True)
        page.set_viewport_size({'width': 375, 'height': 812})
        page.locator('.research-report').evaluate('(element) => element.scrollIntoView({ block: "start" })')
        page.screenshot(path=str(output / 'report-mobile.png'), full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert dialog.evaluate('(element) => element.scrollWidth <= element.clientWidth')
        page.get_by_role('button', name='新建任务', exact=False).click()
        page.locator('#native-run-message').fill('取消等待审核的任务')
        page.get_by_role('button', name='生成研究计划', exact=True).click()
        expect(page.get_by_role('heading', name='审核研究计划')).to_be_visible()
        page.get_by_role('button', name='请求停止', exact=True).click()
        expect(page.locator('.run-meta strong')).to_have_text('已停止')
        page.get_by_role('button', name='新建任务', exact=False).click()
        page.locator('#native-run-message').fill('拒绝新的研究计划')
        page.get_by_role('button', name='生成研究计划', exact=True).click()
        expect(page.get_by_role('heading', name='审核研究计划')).to_be_visible()
        page.get_by_role('button', name='拒绝计划', exact=True).click()
        expect(page.locator('.run-meta strong')).to_have_text('已停止')
        recoverable = new_run('中断的研究任务')
        recoverable.update(status='interrupted', plan_status='approved')
        page.get_by_role('button', name='刷新任务列表', exact=True).click()
        page.locator('.run-item', has_text='中断的研究任务').click()
        page.get_by_role('button', name='继续研究', exact=True).click()
        expect(page.locator('.run-meta strong')).to_have_text('已完成')
        assert any(run.get('parent_run_id') == recoverable['run_id'] for run in state['runs'].values())
        page.keyboard.press('Escape')
        expect(dialog).to_have_count(0)
        expect(page.get_by_role('button', name='研究工作台', exact=True)).to_be_focused()
        assert not errors, errors
        result = {'passed': True, 'browser': 'Edge headless', 'mock_api': True, 'checks': ['research mode', 'draft preserved during polling', 'edit version CAS', '409 refresh', 'approve', 'SSE replay cursor', 'safe report + citations', 'download', '375px responsive', 'cancel awaiting review', 'reject', 'resume interrupted', 'modal focus restore'], 'javascript_errors': errors, 'stream_cursors': state['streams']}
        (output / 'browser-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
        context.close()
        browser.close()


if __name__ == '__main__':
    main()
