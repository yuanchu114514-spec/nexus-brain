"""WebSocket 客户端 — 桌宠与 Hub 的通信通道。

连接 Hub WS，握手声明 client_type: "desktop"。
接收配置、连接状态、shutdown 指令。

心跳检测：每 15s 发 ping，10s 内无 pong → 断线重连。
指数退避重连：3s → 6s → 12s → 24s → max 30s，连接成功后重置。
"""

from __future__ import annotations

import json
import logging
import time
from queue import Queue

from PyQt5.QtCore import QThread, pyqtSignal
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

# ── 重连参数 ──
INITIAL_RECONNECT_INTERVAL = 3.0   # 初始重连间隔（秒）
MAX_RECONNECT_INTERVAL = 30.0      # 最大重连间隔（秒）
BACKOFF_MULTIPLIER = 2.0           # 退避倍数

# ── 心跳参数 ──
HEARTBEAT_INTERVAL = 15.0          # 心跳间隔（秒）
HEARTBEAT_TIMEOUT = 10.0           # 心跳超时——超过此时间未收到 pong 视为断连


class WSClient(QThread):
    """桌宠 WS 客户端。"""

    # 连接状态
    status_changed = pyqtSignal(str)  # "connected" | "reconnecting" | "disconnected"
    # 收到配置
    config_received = pyqtSignal(dict)
    # 服务端要求退出
    shutdown_requested = pyqtSignal(str)

    def __init__(self, url: str = "ws://127.0.0.1:8999/nexus",
                 token: str = "", reconnect_interval: float = INITIAL_RECONNECT_INTERVAL):
        super().__init__()
        self.url = url
        self.token = token
        self._initial_reconnect_interval = reconnect_interval
        self._current_reconnect_interval = reconnect_interval
        self._running = False
        self._send_queue: Queue = Queue()
        self._config: dict | None = None
        # 心跳状态
        self._last_ping: float = 0.0
        self._last_pong: float = 0.0

    @property
    def config(self) -> dict | None:
        return self._config

    @property
    def reconnect_interval(self) -> float:
        """当前重连间隔（随退避动态变化）。"""
        return self._current_reconnect_interval

    def send(self, data: dict):
        """线程安全——将消息放入发送队列。"""
        self._send_queue.put(data)

    def save_position(self, x: int, y: int):
        """发送位置保存请求到 Hub。"""
        self.send({"type": "save_position", "x": x, "y": y})

    def stop(self):
        self._running = False

    def run(self):
        """QThread 主循环 — 指数退避重连。"""
        self._running = True
        self._current_reconnect_interval = self._initial_reconnect_interval
        while self._running:
            try:
                self._connect_loop()
            except Exception as e:
                logger.warning(f"WS 连接异常: {e}")
            if self._running:
                self.status_changed.emit("reconnecting")
                logger.info(
                    f"WS 重连等待 {self._current_reconnect_interval:.0f}s "
                    f"(下次 {min(self._current_reconnect_interval * BACKOFF_MULTIPLIER, MAX_RECONNECT_INTERVAL):.0f}s)"
                )
                time.sleep(self._current_reconnect_interval)
                # 指数退避：每次失败翻倍，上限 30s
                self._current_reconnect_interval = min(
                    self._current_reconnect_interval * BACKOFF_MULTIPLIER,
                    MAX_RECONNECT_INTERVAL,
                )

    def _connect_loop(self):
        with connect(self.url) as ws:
            self.status_changed.emit("connected")
            # 连接成功 → 重置退避间隔
            self._current_reconnect_interval = self._initial_reconnect_interval
            logger.info(f"桌宠已连接 Hub: {self.url}")

            # 握手：声明 desktop 客户端类型
            ws.send(json.dumps({
                "type": "handshake",
                "token": self.token,
                "client_type": "desktop",
            }, ensure_ascii=False))

            # 初始化心跳计时器
            self._last_ping = time.time()
            self._last_pong = time.time()

            while self._running:
                # ── 发送队列 ──
                while not self._send_queue.empty():
                    data = self._send_queue.get_nowait()
                    try:
                        ws.send(json.dumps(data, ensure_ascii=False))
                    except ConnectionClosed:
                        self._send_queue.put(data)
                        raise

                # ── 接收消息（非阻塞，1s 超时）──
                raw = None
                try:
                    raw = ws.recv(timeout=1.0)
                except TimeoutError:
                    pass  # 本周期无消息，继续心跳检查
                except ConnectionClosed:
                    logger.info("WS 连接已关闭")
                    break

                if raw:
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        msg = {}

                    msg_type = msg.get("type", "")

                    if msg_type == "pong":
                        self._last_pong = time.time()
                        # 不 continue —— pong 之后仍需检查心跳发送

                    elif msg_type == "shutdown":
                        reason = msg.get("reason", "unknown")
                        logger.info(f"收到 shutdown: {reason}")
                        self.shutdown_requested.emit(reason)
                        return

                    elif msg_type == "config":
                        self._config = msg.get("data", {})
                        self.config_received.emit(self._config)

                # ── 心跳：定时发送 ping ──
                now = time.time()
                if now - self._last_ping >= HEARTBEAT_INTERVAL:
                    try:
                        ws.send(json.dumps({"type": "ping"}))
                        self._last_ping = now
                        logger.debug(
                            f"WS ping (last pong: {now - self._last_pong:.0f}s ago)"
                        )
                    except ConnectionClosed:
                        break

                # ── 心跳：检测超时 ──
                # 仅当已发送过 ping 且未收到对应 pong 时才判定超时
                # （防止连接后第一个 ping 还没发就误判超时）
                if (self._last_ping > self._last_pong
                        and now - self._last_ping > HEARTBEAT_TIMEOUT):
                    logger.warning(
                        f"WS 心跳超时: {now - self._last_ping:.0f}s 前发送 ping，"
                        f"仍未收到 pong，断开重连"
                    )
                    break

        self.status_changed.emit("disconnected")
