from __future__ import annotations

import base64
import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time


def _enable_reaper() -> None:
    if sys.platform == "linux":
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "无法启用采集进程回收")


def _reap() -> None:
    if sys.platform != "linux":
        return
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def main() -> None:
    _enable_reaper()
    print(json.dumps({"ready": True}), flush=True)
    line = sys.stdin.buffer.readline()
    if not line:
        return
    request = json.loads(line)
    argv = request["argv"]
    if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
        raise ValueError("采集命令必须是非空字符串列表")
    timeout = float(request["timeout"])
    if timeout <= 0:
        raise ValueError("采集超时必须大于零")
    data = base64.b64decode(request["stdin"], validate=True) if request.get("stdin") is not None else None
    cancelled = threading.Event()
    finished = threading.Event()
    lock = threading.RLock()
    proc = None

    def kill_group() -> None:
        if proc is None:
            return
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def cancel(*_args: object) -> None:
        with lock:
            if finished.is_set():
                return
            cancelled.set()
            kill_group()

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, cancel)

    def watch_connection() -> None:
        # SSH 关闭标准输入时取消采集；独立超时同时覆盖连接异常中断。
        while os.read(sys.stdin.fileno(), 8192):
            pass
        cancel()

    threading.Thread(target=watch_connection, daemon=True).start()
    status = "ok"
    with lock:
        if cancelled.is_set():
            status = "cancelled"
        else:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True)
    stdout = stderr = b""
    if proc is not None:
        try:
            stdout, stderr = proc.communicate(data, timeout=timeout)
        except subprocess.TimeoutExpired:
            status = "timeout"
            with lock:
                kill_group()
            stdout, stderr = proc.communicate()
        finally:
            with lock:
                kill_group()
            proc.wait()
            # 确认该进程组已消失后才能答复控制器，防止下一轮继续产生进程。
            while True:
                _reap()
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                kill_group()
                time.sleep(0.05)
            _reap()
    finished.set()
    if cancelled.is_set() and status == "ok":
        status = "cancelled"
    print(json.dumps({"status": status, "cleaned": True,
                      "returncode": proc.returncode if proc is not None else None,
                      "stdout": base64.b64encode(stdout).decode("ascii"),
                      "stderr": base64.b64encode(stderr).decode("ascii")}), flush=True)


if __name__ == "__main__":
    main()
