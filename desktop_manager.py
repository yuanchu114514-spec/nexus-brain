"""桌面端子进程生命周期管理。

基于文件锁的互斥机制，确保全局只有一个桌面 UI 子进程。
插件生成 instance_token，通过 CLI 传给桌面端。
桌面端连接 WS 后发送握手令牌，服务端验证。
令牌不匹配 → 旧桌面端收到 shutdown → 自动退出。

子进程 stderr 通过 PIPE 捕获，逐行转发到 AstrBot 平台日志。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from uuid import uuid4

from astrbot.api import logger

# 文件锁路径（插件目录下），防止跨进程重复 spawn
_LOCK_FILE = Path(__file__).parent / ".desktop.lock"


def _pid_is_alive(pid: int) -> bool:
    """检查 PID 是否仍在运行（Windows 实现）。"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
            timeout=5,
        )
        return str(pid) in result.stdout
    except Exception:
        # 回退：句柄探测法
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
        except Exception:
            pass
        return False


class DesktopManager:
    """管理桌面 UI 子进程的完整生命周期。

    使用 .desktop.lock 文件实现跨进程互斥：
    - spawn() 前获取锁，写入 PID + token
    - 若锁被其他进程持有且 PID 存活 → 拒绝 spawn
    - kill() 时释放锁
    """

    def __init__(self, desktop_main: Path, ws_url: str):
        self._desktop_main = desktop_main
        self._ws_url = ws_url
        self._token = uuid4().hex
        self._proc: subprocess.Popen | None = None
        self._stderr_thread: threading.Thread | None = None

    @property
    def token(self) -> str:
        return self._token

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ═══════════════════════════════════════════════════════════
    # 文件锁（跨进程互斥）
    # ═══════════════════════════════════════════════════════════

    def _acquire_lock(self) -> bool:
        """尝试获取文件锁。若已有存活的桌面进程持有锁，返回 False。"""
        if _LOCK_FILE.exists():
            try:
                data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
                lock_pid = data.get("pid")
                if lock_pid and _pid_is_alive(lock_pid):
                    logger.warning(
                        f"桌面 UI 已在运行中 (PID={lock_pid})，"
                        f"跳过本次 spawn（锁文件: {_LOCK_FILE}）"
                    )
                    return False
                else:
                    # 锁文件残留（进程已死），清理
                    logger.info(f"清理残留锁文件（PID={lock_pid} 已不存在）")
                    _LOCK_FILE.unlink(missing_ok=True)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"锁文件损坏，清理: {e}")
                _LOCK_FILE.unlink(missing_ok=True)

        # 写入锁文件
        try:
            _LOCK_FILE.write_text(
                json.dumps({
                    "pid": os.getpid(),  # 父进程 PID（插件进程），非桌面进程
                    "desktop_token": self._token,
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except OSError as e:
            logger.error(f"无法写入锁文件 {_LOCK_FILE}: {e}")
            return False

    def _release_lock(self):
        """释放文件锁。"""
        try:
            _LOCK_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    # ═══════════════════════════════════════════════════════════
    # 公开 API
    # ═══════════════════════════════════════════════════════════

    def spawn(self) -> bool:
        """启动桌面 UI 子进程（先获取文件锁防止双实例）。"""
        if self.is_running:
            logger.info("桌面 UI 已在运行中，跳过启动")
            return True

        # ── 跨进程互斥检查 ──
        if not self._acquire_lock():
            return False

        if not self._desktop_main.exists():
            logger.error(f"桌面端入口不存在: {self._desktop_main}")
            self._release_lock()
            return False

        # ── 额外保险：扫描并清理同名孤儿进程 ──
        self._kill_orphan_desktop()

        pythonw = self._find_pythonw()

        try:
            self._proc = subprocess.Popen(
                [
                    pythonw,
                    str(self._desktop_main),
                    "--ws-url", self._ws_url,
                    "--token", self._token,
                ],
                cwd=str(self._desktop_main.parent.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            logger.info(f"桌面 UI 子进程已启动: PID={self._proc.pid}")

            # 更新锁文件中的桌面进程 PID
            self._update_lock_desktop_pid(self._proc.pid)

            # 启动 stderr 读取线程，逐行转发到 AstrBot 平台日志
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                daemon=True,
            )
            self._stderr_thread.start()

            return True
        except Exception as e:
            logger.error(f"启动桌面 UI 失败: {e}")
            self._release_lock()
            self._proc = None
            return False

    async def health_check(self) -> bool:
        """短暂等待后检查进程是否存活。"""
        if self._proc is None:
            return False
        await asyncio.sleep(1.0)
        if self._proc.poll() is not None:
            exit_code = self._proc.returncode
            # 读取剩余 stderr 输出（进程已退出，管道中可能还有缓冲数据）
            self._drain_stderr()
            logger.error(
                f"桌面 UI 启动后立即退出 (exit={exit_code})，"
                f"请手动调试: python \"{self._desktop_main}\" "
                f"--ws-url {self._ws_url} --token {self._token}"
            )
            self._release_lock()
            self._proc = None
            return False
        return True

    def kill(self):
        """终止桌面 UI 子进程。先 SIGTERM，5s 后 SIGKILL。"""
        if self._proc is None:
            self._release_lock()
            return
        if self._proc.poll() is not None:
            logger.info("桌面 UI 子进程已退出")
            self._drain_stderr()
            self._stderr_thread = None
            self._proc = None
            self._release_lock()
            return

        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
            logger.info("桌面 UI 子进程已终止")
        except subprocess.TimeoutExpired:
            logger.warning("桌面 UI 子进程未响应 SIGTERM，强制 kill")
            self._proc.kill()
            self._proc.wait(timeout=3)
        self._drain_stderr()
        self._stderr_thread = None
        self._proc = None
        self._release_lock()

    # ═══════════════════════════════════════════════════════════
    # stderr → 平台日志转发
    # ═══════════════════════════════════════════════════════════

    def _read_stderr(self):
        """守护线程：逐行读取子进程 stderr，通过插件 logger 写入平台日志。"""
        try:
            for line in self._proc.stderr:
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    logger.info(f"[desktop] {text}")
        except (ValueError, OSError):
            pass  # 管道已关闭

    def _drain_stderr(self):
        """读取 stderr 管道中残留的数据。"""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            import select
            while select.select([self._proc.stderr], [], [], 0.1)[0]:
                line = self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if text:
                    logger.info(f"[desktop] {text}")
        except (ValueError, OSError, AttributeError):
            pass

    # ═══════════════════════════════════════════════════════════
    # 内部辅助
    # ═══════════════════════════════════════════════════════════

    def _update_lock_desktop_pid(self, pid: int):
        """更新锁文件中的桌面进程 PID。"""
        try:
            data = json.loads(_LOCK_FILE.read_text(encoding="utf-8"))
            data["desktop_pid"] = pid
            _LOCK_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _kill_orphan_desktop(self):
        """扫描并清理残留的同名桌面进程（通过命令行参数匹配）。"""
        try:
            result = subprocess.run(
                ["wmic", "process", "where", "name='pythonw.exe'", "get", "processid,commandline"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if "desktop" in line and "main.py" in line:
                    # 提取 PID（行末的数字）
                    parts = line.strip().split()
                    if parts and parts[-1].isdigit():
                        orphan_pid = int(parts[-1])
                        logger.warning(f"发现残留桌面进程 PID={orphan_pid}，尝试终止")
                        try:
                            subprocess.run(
                                ["taskkill", "/PID", str(orphan_pid), "/F"],
                                capture_output=True, timeout=10,
                            )
                        except Exception:
                            pass
        except Exception:
            pass  # wmic 失败时静默跳过

    @staticmethod
    def _find_pythonw() -> str:
        """查找 pythonw.exe（无黑窗），回退到 python.exe。"""
        py_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(py_dir, "pythonw.exe")
        if os.path.exists(pythonw):
            return pythonw
        logger.warning("pythonw.exe 未找到，回退 python.exe（可能闪现终端）")
        return sys.executable
