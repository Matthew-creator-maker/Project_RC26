"""语音服务端口保护：启动前释放被占用的 TCP 端口。

三个语音服务端（ASR/TTS/MIC）在 ``if __name__ == "__main__"`` 启动 uvicorn 前调用
:func:`ensure_port_free`，避免旧服务残留导致端口冲突。
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import time
from typing import Iterable


def is_port_open(port: int) -> bool:
    """通过尝试连接 127.0.0.1:port 判断端口是否已被 TCP 服务占用。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False


def _pids_by_ss(port: int) -> set[int]:
    result = subprocess.run(
        ["ss", "-ltnp"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    pids: set[int] = set()
    token = f":{port}"
    for line in result.stdout.splitlines():
        if token not in line:
            continue
        pids.update(int(match.group(1)) for match in re.finditer(r"pid=(\d+)", line))
    return pids


def _pids_by_lsof(port: int) -> set[int]:
    result = subprocess.run(
        ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
    )
    return {int(pid) for pid in result.stdout.split() if pid.isdigit()}


def _port_pids(port: int) -> set[int]:
    if shutil.which("ss"):
        try:
            return _pids_by_ss(port)
        except Exception as exc:  # 诊断工具失败不阻断启动，走后续兜底
            print(f"[PORT-GUARD] ss 查询失败: {exc}")
    if shutil.which("lsof"):
        try:
            return _pids_by_lsof(port)
        except Exception as exc:
            print(f"[PORT-GUARD] lsof 查询失败: {exc}")
    return set()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def ensure_port_free(port: int, service_name: str) -> None:
    """若端口被占用，则先终止占用进程，再继续启动当前服务。"""
    if not is_port_open(port):
        print(f"[{service_name}] 端口 {port} 空闲，直接启动")
        return

    pids = _port_pids(port)
    pids.discard(os.getpid())

    if pids:
        print(f"[{service_name}] 端口 {port} 被占用，占用 PID: "
              f"{','.join(str(pid) for pid in sorted(pids))}，准备 pkill")
        for pid in sorted(pids):
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"[{service_name}] 已发送 SIGTERM -> PID {pid}")
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                print(f"[{service_name}] 无法结束 PID {pid}: {exc}")
        time.sleep(1.0)

        remaining = {pid for pid in pids if _pid_exists(pid)}
        for pid in sorted(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
                print(f"[{service_name}] 强杀仍存活的 PID {pid}")
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.5)
    else:
        print(f"[{service_name}] 端口 {port} 被占用，但未解析到 PID，尝试 fuser -k")
        if shutil.which("fuser"):
            subprocess.run(["fuser", "-k", f"{port}/tcp"], check=False)
            time.sleep(1.0)

    if is_port_open(port):
        print(f"[{service_name}] 警告：端口 {port} 仍被占用，uvicorn 启动可能失败")
    else:
        print(f"[{service_name}] 端口 {port} 已释放，继续启动")
