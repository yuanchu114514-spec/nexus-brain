"""Nexus 桌面端子进程入口。

Layer 1: MiniWindow (迷你头像, 默认常驻)
Layer 2-3: 待 Phase 3 实现 (Live2D + 搭话气泡)

由 Nexus Brain 插件通过 subprocess 启动，WS 接收配置。
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import math
import os
import subprocess
import sys
import webbrowser
from ctypes import wintypes
from pathlib import Path

# ── 日志配置 ──
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon, QPixmap, QPainter, QPainterPath, QColor, QFont
from PyQt5.QtCore import Qt, QTimer

# 确保能导入同目录模块
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from mini_window import MiniWindow
from ws_client import WSClient
from settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)


def _make_tray_icon() -> QIcon:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)

    # White circular base
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawEllipse(2, 2, 28, 28)

    # Blue four-pointed star (8 vertices, alternating outer/inner radii)
    cx = cy = 16.0
    r_outer = 12.0
    r_inner = r_outer * 0.38

    path = QPainterPath()
    for i in range(8):
        angle = -math.pi / 2.0 + i * math.pi / 4.0
        r = r_outer if i % 2 == 0 else r_inner
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        if i == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()

    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#5B9BD5"))
    p.drawPath(path)

    p.end()
    return QIcon(pm)


def _find_launcher_exe() -> str | None:
    """定位 astrbot-launcher.exe 路径（从运行中进程获取，或检查默认安装位置）。"""
    # 策略 A：从运行中的进程获取完整路径
    kernel32 = ctypes.windll.kernel32

    TH32CS_SNAPPROCESS = 2
    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize",              wintypes.DWORD),
            ("cntUsage",            wintypes.DWORD),
            ("th32ProcessID",       wintypes.DWORD),
            ("th32DefaultHeapID",   ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID",        wintypes.DWORD),
            ("cntThreads",          wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase",      wintypes.LONG),
            ("dwFlags",             wintypes.DWORD),
            ("szExeFile",           wintypes.WCHAR * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap is not None and snap != INVALID_HANDLE_VALUE:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                exe_name = pe.szExeFile.lower()
                if "astrbot" in exe_name and "launcher" in exe_name:
                    pid = pe.th32ProcessID
                    # QueryFullProcessImageNameW 获取完整路径
                    PROCESS_QUERY_LIMITED_INFO = 0x1000
                    hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFO, False, pid)
                    if hproc:
                        buf = ctypes.create_unicode_buffer(260)
                        size = wintypes.DWORD(260)
                        # QueryFullProcessImageNameW
                        if kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(size)):
                            kernel32.CloseHandle(hproc)
                            kernel32.CloseHandle(snap)
                            return buf.value
                        kernel32.CloseHandle(hproc)
                if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        kernel32.CloseHandle(snap)

    # 策略 B：检查默认安装位置
    default_path = os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "AstrBot Launcher",
        "astrbot-launcher.exe",
    )
    if os.path.isfile(default_path):
        return default_path

    return None


def _open_chat(web_chat_url: str):
    """打开聊天界面：启动 astrbot-launcher.exe（单实例 Electron，再次启动 = 弹出现有窗口）。"""
    launcher_exe = _find_launcher_exe()
    if launcher_exe:
        try:
            logger.debug(f"启动 Launcher: {launcher_exe}")
            subprocess.Popen(
                [launcher_exe],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        except Exception as exc:
            logger.warning(f"启动 Launcher 失败: {exc}")

    # 回退：用浏览器打开 Web Chat
    logger.debug(f"回退：浏览器打开 {web_chat_url}")
    webbrowser.open(web_chat_url)


def _create_tray(char_name: str, web_chat_url: str,
                 show_mini_fn, quit_fn) -> QSystemTrayIcon:
    """创建系统托盘图标和菜单。"""
    tray = QSystemTrayIcon()
    tray.setIcon(_make_tray_icon())
    tray.setToolTip(f"{char_name}桌面伴侣")

    tray_menu = QMenu()

    web_chat_action = QAction("💬 打开 Web Chat")
    web_chat_action.triggered.connect(lambda: QTimer.singleShot(100, lambda: _open_chat(web_chat_url)))
    tray_menu.addAction(web_chat_action)

    show_mini_action = QAction("⭐ 显示悬浮窗")
    show_mini_action.triggered.connect(show_mini_fn)
    tray_menu.addAction(show_mini_action)

    tray_menu.addSeparator()

    quit_action = QAction("❌ 退出")
    quit_action.triggered.connect(quit_fn)
    tray_menu.addAction(quit_action)

    tray.setContextMenu(tray_menu)

    def on_activate(reason):
        if reason == QSystemTrayIcon.DoubleClick:
            QTimer.singleShot(100, lambda: _open_chat(web_chat_url))

    tray.activated.connect(on_activate)
    tray.show()
    return tray


def main():
    parser = argparse.ArgumentParser(description="Nexus 桌面宠物")
    parser.add_argument("--ws-url", default="ws://127.0.0.1:8999/nexus",
                        help="WebSocket Hub 地址")
    parser.add_argument("--token", default="",
                        help="实例令牌（握手验证）")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    mini = MiniWindow()
    tray: QSystemTrayIcon | None = None
    ws: WSClient | None = None
    char_name = "Nexus"
    web_chat_url = "http://localhost:8080"

    # ── 退出清理 ──
    def _do_quit():
        nonlocal ws, tray, mini
        pos = mini.save_position()
        if ws and ws.isRunning():
            ws.save_position(pos["x"], pos["y"])
        if ws:
            ws.stop()
            ws.wait(3000)
        if tray:
            tray.hide()
        mini.hide()
        app.quit()

    # ── shutdown 指令 ──
    def _on_shutdown(reason: str):
        logger.info(f"Hub 要求退出: {reason}")
        _do_quit()

    # ── 收到配置后初始化 ──
    def on_config(cfg: dict):
        nonlocal tray, char_name, web_chat_url

        char_name = cfg.get("character", {}).get("name", "Nexus")
        web_chat_url = cfg.get("hub", {}).get("web_chat_url", "http://localhost:8080")
        memory_folder = cfg.get("memory", {}).get("folder_path", "")

        mini.set_web_chat_url(web_chat_url)
        mini.set_char_name(char_name)
        mini.set_memory_folder(memory_folder)

        mw = cfg.get("mini_window", {})
        mini.restore_position(mw.get("x"), mw.get("y"))

        tray = _create_tray(
            char_name,
            web_chat_url=web_chat_url,
            show_mini_fn=lambda: mini.show(),
            quit_fn=_do_quit,
        )

        # 首次启动引导
        desktop_cfg = cfg.get("desktop", {})
        if desktop_cfg.get("first_run", True):
            tray.showMessage(
                f"{char_name}来啦~",
                f"桌面上的蓝色星星就是{char_name}~\n"
                "点击星星打开 Web Chat 聊天\n"
                "右键星星或托盘图标找到更多选项\n"
                "右键星星 → 个性化设置 修改角色名和记忆文件夹",
                QSystemTrayIcon.Information,
                5000,
            )

    def _open_settings():
        nonlocal char_name
        dialog = SettingsDialog(
            parent=None,
            char_name=char_name,
            memory_folder=mini._memory_folder,
        )
        if dialog.exec_():
            new_name, new_folder = dialog.get_values()
            char_name = new_name
            mini.set_char_name(new_name)
            mini.set_memory_folder(new_folder)
            if tray:
                tray.setToolTip(f"{char_name}桌面伴侣")
            # Notify Hub to save config
            if ws and ws.isRunning():
                ws.send({
                    "type": "update_config",
                    "character": {"name": new_name},
                    "memory": {"folder_path": new_folder},
                })

    # ── 信号连接 ──
    # Phase 1: 点击星星 / 右键「打开 Web Chat」→ 激活 Launcher 窗口（回退浏览器）
    # 用 QTimer 延迟 100ms 确保右键菜单先关闭，避免 SetForegroundWindow 竞态
    mini.clicked.connect(lambda: QTimer.singleShot(100, lambda: _open_chat(web_chat_url)))
    mini.open_chat_requested.connect(lambda: QTimer.singleShot(100, lambda: _open_chat(web_chat_url)))
    mini.hide_requested.connect(mini.hide)
    mini.quit_requested.connect(_do_quit)
    mini.settings_requested.connect(_open_settings)

    # ── WS 状态 ──
    def on_status(status: str):
        # TODO Phase 3: 呼吸灯连接状态指示
        if status == "connected":
            logger.info("悬浮窗已连接 Hub")
        elif status == "disconnected":
            logger.warning("悬浮窗与 Hub 断开")

    # ── 启动 WS ──
    ws = WSClient(url=args.ws_url, token=args.token, reconnect_interval=3.0)
    ws.config_received.connect(on_config)
    ws.status_changed.connect(on_status)
    ws.shutdown_requested.connect(_on_shutdown)
    ws.start()

    mini.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
