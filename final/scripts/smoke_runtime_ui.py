"""Opt-in live acceptance; creates one disposable user and makes two model calls.

Run against a local development server with public registration enabled.
No credentials are printed or written. The smoke user's data remains for review.
Requires Playwright and an installed browser; use --channel msedge on Windows.
"""

import argparse
import json
import re
import secrets
import time
from pathlib import Path
from uuid import uuid4


def main():
    from playwright.sync_api import expect, sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8090")
    parser.add_argument("--channel", default="msedge")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    with sync_playwright() as playwright:
        api = playwright.request.new_context(base_url=base, timeout=args.timeout * 1000)
        assert api.get("/healthz").ok, "Server health check failed"
        username = "runtime_smoke_" + uuid4().hex[:12]
        registered = api.post("/api/auth/register", data={"username": username, "password": secrets.token_urlsafe(24)})
        assert registered.ok, f"Smoke registration HTTP {registered.status}"
        identity = registered.json()
        headers = {"Authorization": "Bearer " + identity["token"]}
        browser = playwright.chromium.launch(headless=True, channel=args.channel or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.add_init_script(
            "localStorage.setItem('agi_auth_token', " + json.dumps(identity["token"]) + ");"
            "localStorage.setItem('agi_auth_user', " + json.dumps(username) + ");"
        )
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base, wait_until="networkidle")
        page.get_by_role("button", name=re.compile(r"^RUN\s*运行记录$")).click()
        dialog = page.get_by_role("dialog", name="Saber 运行记录")
        expect(dialog).to_be_visible()
        page.get_by_label("任务目标", exact=True).fill("请只回复：Saber 原生后台验收通过。不要调用工具。")
        with page.expect_response(
            lambda response: response.url.endswith("/api/agent-runs") and response.request.method == "POST"
        ) as created:
            page.get_by_role("button", name="开始运行", exact=True).click()
        response = created.value
        assert response.ok, f"Create run HTTP {response.status}"
        run_id = response.json()["run_id"]
        page.get_by_role("button", name="关闭运行记录", exact=True).click()
        expect(dialog).not_to_be_visible()
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            run_response = api.get(f"/api/agent-runs/{run_id}", headers=headers)
            assert run_response.ok
            run = run_response.json()
            if run["status"] not in {"pending", "running", "cancelling"}:
                break
            time.sleep(0.5)
        assert run["status"] == "completed", f"Background status: {run['status']}"
        assert run["result"]["response"]["answer"].strip(), "Empty model answer"
        page.reload(wait_until="networkidle")
        page.get_by_role("button", name=re.compile(r"^RUN\s*运行记录$")).click()
        expect(dialog.locator(".answer pre")).to_have_text(run["result"]["response"]["answer"])
        expect(dialog.locator(".run-meta")).to_contain_text("已完成")

        foreground = api.post(
            "/api/chat/stream",
            headers=headers,
            data={"message": "请只回复：Saber 前台验收通过。不要调用工具。", "conversation_id": uuid4().hex},
        )
        assert foreground.ok and "event: done" in foreground.text()
        foreground_id = foreground.headers["x-saber-run-id"]
        observed = api.get(f"/api/agent-runs/{foreground_id}", headers=headers).json()
        assert observed["status"] == "completed" and observed["kind"] == "chat_stream"
        events = api.get(f"/api/agent-runs/{foreground_id}/events", headers=headers).json()
        assert sum(event["type"] == "done" for event in events) == 1
        page.get_by_role("button", name="刷新任务列表").click()
        expect(dialog.locator(".run-item")).to_have_count(2)
        assert not errors, "Browser errors: " + "; ".join(errors)
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(args.screenshot), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("button", name="关闭运行记录")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile page overflows"
        if args.screenshot:
            page.screenshot(path=str(args.screenshot.with_stem(args.screenshot.stem + "-mobile")), full_page=True)
        page.get_by_role("button", name="关闭运行记录", exact=True).click()
        page.get_by_role("button", name="文件与会话", exact=True).click()
        expect(page.locator("#sidebar-navigation")).to_be_visible()
        page.get_by_role("button", name="关闭文件与会话", exact=True).click()
        expect(page.locator("#sidebar-navigation")).not_to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile chat overflows"
        print(
            json.dumps(
                {
                    "background": run_id,
                    "foreground": foreground_id,
                    "status": "passed",
                    "browser_errors": len(errors),
                    "history_count": 2,
                    "smoke_user": username,
                },
                ensure_ascii=False,
            )
        )
        context.close()
        browser.close()
        api.dispose()


if __name__ == "__main__":
    main()
