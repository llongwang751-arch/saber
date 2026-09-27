"""Exercise real child-process I/O/lifetime without needing a Docker daemon."""

import subprocess
import sys
import threading

import pytest

from internal.agent.cancel import CancelToken
from internal.sandbox.docker import DockerSandbox
from internal.sandbox.types import ExecRequest, SandboxConfig


@pytest.fixture
def docker_cli(monkeypatch):
    real_popen = subprocess.Popen
    calls = []
    children = []
    scripts = {"create": "print('container-id')", "start": "print('42')"}

    def popen(args, **kwargs):
        calls.append(args)
        process = real_popen([sys.executable, "-u", "-c", scripts[args[1]]], **kwargs)
        children.append(process)
        return process

    def run(args, **kwargs):
        calls.append(args)
        assert args[:3] == ["docker", "rm", "--force"]
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(DockerSandbox, "_probe", lambda self: True)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", run)
    yield scripts, calls
    assert all(child.poll() is not None for child in children), "Docker CLI child left running"


def test_execution_uses_named_lifecycle_and_preserves_windows_mount(docker_cli):
    scripts, calls = docker_cli
    sandbox = DockerSandbox(SandboxConfig(image="python:3.11-slim"))
    workspace = r"C:\Users\测试 用户\AppData\Local\Temp\research-123"
    result = sandbox.exec(None, ExecRequest(command="python3 /workspace/analysis.py", workspace_host_dir=workspace))
    assert result.exit_code == 0 and result.stdout.strip() == "42"
    assert [call[1] for call in calls] == ["create", "start", "rm"]
    create = calls[0]
    name = create[create.index("--name") + 1]
    assert name.startswith("saber-sandbox-")
    assert calls[1] == ["docker", "start", "--attach", name]
    assert calls[-1] == ["docker", "rm", "--force", name]
    assert create[create.index("--mount") + 1] == f"type=bind,src={workspace},dst=/workspace"
    assert "--read-only" in create and create[create.index("--network") + 1] == "none"


def test_timeout_kills_cli_and_removes_container(docker_cli):
    scripts, calls = docker_cli
    scripts["start"] = "import time; print('started'); time.sleep(60)"
    result = DockerSandbox(SandboxConfig()).exec(None, ExecRequest(command="sleep 60", timeout=0.5))
    assert result.killed and result.exit_code == -4
    assert "超时" in result.stderr
    assert calls[-1][1:3] == ["rm", "--force"]
    assert result.duration < 3


def test_cancellation_interrupts_running_container(docker_cli):
    scripts, calls = docker_cli
    scripts["start"] = "import time; print('started'); time.sleep(60)"
    token = CancelToken()
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    try:
        result = DockerSandbox(SandboxConfig()).exec(token, ExecRequest(command="sleep 60", timeout=30))
    finally:
        timer.cancel()
    assert result.killed and result.exit_code == -6
    assert "取消" in result.stderr
    assert calls[-1][1:3] == ["rm", "--force"]
    assert result.duration < 3


def test_precancelled_request_never_creates_container(docker_cli):
    scripts, calls = docker_cli
    token = CancelToken()
    token.cancel()
    result = DockerSandbox(SandboxConfig()).exec(token, ExecRequest(command="sleep 60"))
    assert result.killed and result.exit_code == -6
    assert calls == []


def test_large_output_is_drained_but_only_bounded_bytes_are_retained(docker_cli):
    scripts, calls = docker_cli
    scripts["start"] = "import sys; sys.stdout.write('A'*100000); sys.stderr.write('B'*100000)"
    result = DockerSandbox(SandboxConfig(max_output_bytes=32)).exec(None, ExecRequest(command="generate"))
    assert result.exit_code == 0 and result.truncated
    assert result.stdout == "A" * 32 and result.stderr == "B" * 32


def test_failed_create_never_starts_user_code(docker_cli):
    scripts, calls = docker_cli
    scripts["create"] = "import sys; sys.stderr.write('image unavailable'); sys.exit(125)"
    result = DockerSandbox(SandboxConfig()).exec(None, ExecRequest(command="generate"))
    assert result.exit_code == 125 and result.stderr == "image unavailable"
    assert [call[1] for call in calls] == ["create", "rm"]


def test_timeout_while_creating_never_starts_user_code(docker_cli):
    scripts, calls = docker_cli
    scripts["create"] = "import time; time.sleep(60)"
    result = DockerSandbox(SandboxConfig()).exec(None, ExecRequest(command="generate", timeout=0.2))
    assert result.killed and result.exit_code == -4
    assert [call[1] for call in calls] == ["create", "rm"]


def test_nonzero_analysis_exit_is_preserved(docker_cli):
    scripts, calls = docker_cli
    scripts["start"] = "import sys; sys.stderr.write('analysis failed'); sys.exit(7)"
    result = DockerSandbox(SandboxConfig()).exec(None, ExecRequest(command="generate"))
    assert result.exit_code == 7 and result.stderr == "analysis failed"


def test_cleanup_failure_cannot_be_reported_as_success(docker_cli, monkeypatch):
    scripts, calls = docker_cli
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 1, b"", b"daemon disconnected"))
    result = DockerSandbox(SandboxConfig()).exec(None, ExecRequest(command="generate"))
    assert result.exit_code == -5 and "清理失败" in result.stderr
