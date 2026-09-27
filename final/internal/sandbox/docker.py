# sandbox.docker — 通过 docker CLI 在隔离容器内执行命令
import logging
import os
import subprocess
import threading
import time
from typing import List
from uuid import uuid4

from .types import ExecRequest, ExecResult, SandboxConfig

logger = logging.getLogger(__name__)


def _container_user() -> str:
    """Linux 上以宿主同 uid/gid 运行容器，避免容器内 root 写宿主文件。

    Windows/macOS 的 Docker Desktop 由 vm 层做 uid 映射，返回空串跳过。
    """
    if os.name != "posix":
        return ""
    try:
        return f"{os.getuid()}:{os.getgid()}"
    except Exception:
        return ""


def _truncate_bytes(data: bytes, max_bytes: int) -> tuple:
    """按字节数截断，返回 (截断后字符串, 是否被截断)。"""
    if not data:
        return "", False
    if max_bytes <= 0:
        return data.decode("utf-8", errors="replace"), False
    if len(data) <= max_bytes:
        return data.decode("utf-8", errors="replace"), False
    return data[:max_bytes].decode("utf-8", errors="replace"), True


class DockerSandbox:
    """通过 docker CLI 执行命令的沙箱后端

    关键安全约束（作为 docker run 参数传入）:
        --rm                  执行完自动清理容器
        --network none        禁用网络
        --read-only           根文件系统只读
        --tmpfs /tmp:size=...允许 /tmp 临时写入
        --memory / --cpus / --pids-limit  cgroup 资源硬限制
        --security-opt no-new-privileges  禁止权限提升
        --cap-drop ALL        放弃所有 Linux capabilities
    """

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._available = self._probe()

    def backend(self) -> str:
        return "docker"

    def available(self) -> bool:
        return self._available

    def _probe(self) -> bool:
        """通过 docker version 检测 daemon 是否就绪（1.5s 超时）"""
        try:
            proc = subprocess.run(
                ["docker", "version", "--format", "{{.Server.Version}}"],
                capture_output=True,
                timeout=1.5,
                check=False,
            )
            return proc.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
        except Exception:
            return False

    def exec(self, ctx, req: ExecRequest) -> ExecResult:
        start = time.monotonic()
        result = ExecResult(command=req.command, backend="docker")

        if not self._available:
            result.exit_code = -3
            result.stderr = "Docker 后端不可用"
            return result

        timeout = req.timeout if req.timeout > 0 else self.cfg.timeout
        if timeout <= 0:
            timeout = 30.0

        is_cancelled = getattr(ctx, "is_cancelled", lambda: False)
        if callable(is_cancelled) and is_cancelled():
            result.exit_code = -6
            result.killed = True
            result.stderr = "[取消] 执行已取消"
            return result

        # Create first, then attach/start. A cancelled create cannot start user
        # code after cleanup; killing only `docker run` would leave it running.
        name = "saber-sandbox-" + uuid4().hex
        args = self._build_docker_args(req.command, req.workspace_host_dir)
        args[0] = "create"
        # Own removal explicitly so Docker's asynchronous --rm cannot race our
        # final cleanup and turn a successful execution into a cleanup error.
        args.remove("--rm")
        args[1:1] = ["--name", name]
        deadline = start + timeout

        try:
            outcome = self._run_cli(args, deadline, ctx)
            if outcome[0] == 0 and not outcome[4]:
                outcome = self._run_cli(["start", "--attach", name], deadline, ctx)
            result.exit_code, stdout, stderr, result.truncated, stopped = outcome
            result.stdout, _ = _truncate_bytes(stdout, self.cfg.max_output_bytes)
            result.stderr, _ = _truncate_bytes(stderr, self.cfg.max_output_bytes)
            if stopped:
                result.killed = True
                result.exit_code = -6 if stopped == "cancelled" else -4
                result.stderr += ("\n[取消] 执行已取消" if stopped == "cancelled"
                                  else f"\n[超时] 执行超过 {timeout}s 被强制终止")
        except FileNotFoundError:
            result.exit_code = -3
            result.stderr = "Docker CLI 不可用"
        except Exception as e:
            result.exit_code = -5
            result.stderr += f"\n[沙箱内部错误] {e}"
        finally:
            # Explicit removal kills a running container on timeout,
            # cancellation, or an interrupted CLI attach, as well as normal exit.
            try:
                cleanup = subprocess.run(
                    ["docker", "rm", "--force", name], capture_output=True,
                    timeout=5, check=False,
                )
                if cleanup.returncode and b"No such container" not in cleanup.stderr:
                    result.stderr += "\n[清理失败] 无法确认隔离容器已删除"
                    if result.exit_code == 0:
                        result.exit_code = -5
            except (OSError, subprocess.TimeoutExpired):
                result.stderr += "\n[清理失败] 无法确认隔离容器已删除"
                if result.exit_code == 0:
                    result.exit_code = -5
            result.duration = time.monotonic() - start

        return result

    def _run_cli(self, args, deadline, ctx):
        """Drain both pipes with bounded buffers while polling cancellation."""
        is_cancelled = getattr(ctx, "is_cancelled", lambda: False)
        if callable(is_cancelled) and is_cancelled():
            return -6, b"", b"", False, "cancelled"
        if time.monotonic() >= deadline:
            return -4, b"", b"", False, "timeout"
        proc = subprocess.Popen(
            ["docker"] + args, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        buffers = [bytearray(), bytearray()]
        truncated = [False, False]

        def drain(pipe, index):
            try:
                while chunk := pipe.read(8192):
                    limit = self.cfg.max_output_bytes
                    keep = len(chunk) if limit <= 0 else min(len(chunk), max(0, limit - len(buffers[index])))
                    buffers[index].extend(chunk[:keep])
                    truncated[index] |= keep < len(chunk)
            finally:
                pipe.close()

        readers = [threading.Thread(target=drain, args=(pipe, i), daemon=True)
                   for i, pipe in enumerate((proc.stdout, proc.stderr))]
        for reader in readers:
            reader.start()
        stopped = ""
        try:
            while proc.poll() is None:
                if callable(is_cancelled) and is_cancelled():
                    stopped = "cancelled"
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    stopped = "timeout"
                    break
                try:
                    proc.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=5)
        return proc.returncode, bytes(buffers[0]), bytes(buffers[1]), any(truncated), stopped

    def _build_docker_args(self, command: str, workspace_host_dir: str = "") -> List[str]:
        """构造 docker run 的完整参数列表"""
        args = [
            "run",
            "--rm",
            "-i",
            "--security-opt", "no-new-privileges",
            "--cap-drop", "ALL",
        ]

        run_as = _container_user()
        if run_as:
            args += ["--user", run_as]

        if self.cfg.network_disabled:
            args += ["--network", "none"]
        if self.cfg.readonly_rootfs:
            args += ["--read-only", "--tmpfs", "/tmp:rw,size=64m"]
        if self.cfg.memory_limit_mb > 0:
            args += ["--memory", f"{self.cfg.memory_limit_mb}m"]
        if self.cfg.cpu_percent > 0:
            # docker 的 --cpus 接受小数核心数；50% → 0.5
            args += ["--cpus", f"{self.cfg.cpu_percent / 100.0:.2f}"]
        if self.cfg.max_pids > 0:
            args += ["--pids-limit", str(self.cfg.max_pids)]
        if workspace_host_dir:
            args += ["--mount", f"type=bind,src={workspace_host_dir},dst=/workspace", "-w", "/workspace"]

        image = self.cfg.image or "ubuntu:22.04"
        args += [image, "sh", "-c", command]
        return args
