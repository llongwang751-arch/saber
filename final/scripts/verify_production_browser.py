"""Read-only cloud UI check. Tokens/cookies remain in process memory."""
import json
from pathlib import Path
import re
import subprocess
from playwright.sync_api import sync_playwright, expect
from deploy_worker_datastores import SSH, CONTROL

BOOTSTRAP = r'''
import json,subprocess
from pathlib import Path
import requests
obj=json.loads(subprocess.check_output(["docker","inspect","mini-drop-control-apiserver-1"]))[0]
env=dict(item.split("=",1) for item in obj["Config"]["Env"] if "=" in item)
cert=next(m["Source"] for m in obj["Mounts"] if m["Destination"]=="/certs")
s=requests.Session();s.verify=str(Path(cert)/"ca.crt");s.trust_env=False
base="https://120.24.187.205"
r=s.post(base+"/api/auth/session",headers={"X-API-Key":env["MINI_DROP_API_KEY"]},timeout=10);r.raise_for_status()
r=s.post(base+"/api/office/api/auth/login",json=json.loads(Path("/etc/agi-office/acceptance-account.json").read_text()),timeout=10);r.raise_for_status()
cookies=[{"name":c.name,"value":c.value,"domain":c.domain,"path":c.path,"secure":c.secure,"httpOnly":True,"sameSite":"Lax"} for c in s.cookies]
print(json.dumps({"cookies":cookies,"identity":r.json()}))
'''


def main():
    result = subprocess.run(SSH + [CONTROL, "/opt/agi-office/venvs/native-20260924/bin/python -"],
                            input=BOOTSTRAP, text=True, encoding="utf-8", capture_output=True,
                            check=True, timeout=30)
    login = json.loads(result.stdout)
    output = Path(__file__).resolve().parents[1] / "runtime/production-acceptance"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="msedge")
        context = browser.new_context(viewport={"width":1440,"height":1000})
        context.add_cookies(login["cookies"])
        identity = login["identity"]
        context.add_init_script("localStorage.setItem('agi_auth_token'," + json.dumps(identity["token"]) + ");"
                                "localStorage.setItem('agi_auth_user'," + json.dumps(json.dumps(identity.get("user", {}))) + ");")
        page = context.new_page()
        errors, failures = [], []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("response", lambda r: failures.append({"url":r.url.split("?")[0],"status":r.status})
                if "/api/office/" in r.url and r.status >= 400 else None)
        response = page.goto("https://120.24.187.205/api/office/", wait_until="networkidle", timeout=45000)
        assert response.ok
        page.get_by_role("button", name=re.compile(r"^RUN\s*运行记录$")).click()
        dialog = page.get_by_role("dialog", name="Saber 运行记录")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".run-item").first).to_be_visible()
        page.screenshot(path=str(output / "production-desktop.png"), full_page=True)
        page.get_by_role("button", name="关闭运行记录", exact=True).click()
        page.set_viewport_size({"width":390,"height":844})
        page.screenshot(path=str(output / "production-mobile.png"), full_page=True)
        assert not errors and not failures, "Browser errors or failed app requests"
        evidence = {"passed":True, "browser":"Edge headless", "tls_verification":True,
                    "run_workbench_visible":True, "javascript_errors":errors, "failed_requests":failures,
                    "screenshots":["production-desktop.png","production-mobile.png"]}
        (output / "browser-acceptance.json").write_text(json.dumps(evidence,indent=2), encoding="utf-8")
        print(json.dumps(evidence))
        context.close()
        browser.close()


if __name__ == "__main__":
    main()

