"""WebSocket 客户端 — 桌宠与 Hub 的通信通道。

连接 Hub WS，握手声明 client_type: "desktop"。
接收配置、连接状态、shutdown 指令。
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


class WSClient(QThread):
    """桌宠 WS 客户端。"""

    # 连接状态
    status_changed = pyqtSignal(str)  # "connected" | "reconnecting" | "disconnected"
    # 收到配置
    config_received = pyqtSignal(dict)
    # 服务端要求退出
    shutdown_requested = pyqtSignal(str)

    def __init__(self, url: str = "ws://127.0.0.1:8999/nexus",
                 token: str = "", reconnect_interval: float = 3.0):
        super().__init__()
        self.url = url
        self.token = token
        self.reconnect_interval = reconnect_interval
        self._running = False
        self._send_queue: Queue = Queue()
        self._config: dict | None = None

    @property
    def config(self) -> dict | None:
        return self._config

    def send(self, data: dict):
        """线程安全——将消息放入发送队列。"""
        self._send_queue.put(data)

    def save_position(self, x: int, y: int):
        """发送位置保存请求到 Hub。"""
        self.send({"type": "save_position", "x": x, "y": y})

    def stop(self):
        self._running = False

    def run(self):
        """QThread 主循环。"""
        self._running = True
        while self._running:
            try:
                self._connect_loop()
            except Exception as e:
                logger.warning(f"WS 连接异常: {e}")
            if self._running:
                self.status_changed.emit("reconnecting")
                time.sleep(self.reconnect_interval)

    def _connect_loop(self):
        with connect(self.url) as ws:
            self.status_changed.emit("connected")
            logger.info(f"桌宠已连接 Hub: {self.url}")

            # 握手：声明 desktop 客户端类型
            ws.send(json.dumps({
                "type": "handshake",
                "token": self.token,
                "client_type": "desktop",
            }, ensure_ascii=False))

            while self._running:
                # 发送队列
                while not self._send_queue.empty():
                    data = self._send_queue.get_nowait()
                    try:
                        ws.send(json.dumps(data, ensure_ascii=False))
                    except ConnectionClosed:
                        self._send_queue.put(data)
                        raise

                # 接收消息
                try:
                    raw = ws.recv(timeout=0.1)
                except TimeoutError:
                    continue
                except ConnectionClosed:
                    logger.info("WS 连接已关闭")
                    break

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "shutdown":
                    reason = msg.get("reason", "unknown")
                    logger.info(f"收到 shutdown: {reason}")
                    self.shutdown_requested.emit(reason)
                    return

                if msg_type == "config":
                    self._config = msg.get("data", {})
                    self.config_received.emit(self._config)
                    continue

                # ping/pong
                if msg_type == "pong":
                    continue

        self.status_changed.emit("disconnected")
