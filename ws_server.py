"""WebSocket 服务端 — 与桌面 Nexus 客户端的双向通信通道。

客户端连接时必须先发送握手令牌（handshake），验证通过后下发配置。
令牌不匹配的旧实例会收到 shutdown 消息后断开。
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional, Callable, Awaitable

import websockets
from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from astrbot.api import logger

MessageHandler = Callable[[dict, ServerConnection], Awaitable[None]]


class NexusWSServer:
    """WebSocket 服务端，管理桌面 Nexus 客户端的连接。

    同一时间只允许一个桌面端连接。新连接会踢掉旧连接。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8999, path: str = "/nexus"):
        self.host = host
        self.port = port
        self.path = path
        self._server: Optional[websockets.Server] = None
        self._handler: Optional[MessageHandler] = None
        self._config: dict = {}
        self._token: str = ""
        self._active_ws: Optional[ServerConnection] = None
        self._satellite_ws: dict[str, ServerConnection] = {}  # platform → ws

    def set_config(self, config: dict):
        self._config = config

    def set_token(self, token: str):
        """设置实例令牌，桌面端握手时必须匹配。"""
        self._token = token

    def on_message(self, handler: MessageHandler):
        self._handler = handler

    async def start(self):
        """启动 WS 服务器。"""
        self._server = await websockets.serve(
            self._handle_connection,
            self.host,
            self.port,
            process_request=self._check_path,
        )
        logger.info(f"NexusBrain WS 服务已启动: ws://{self.host}:{self.port}{self.path}")

    async def stop(self):
        """停止 WS 服务器，踢掉当前桌面端。"""
        if self._active_ws:
            try:
                await self.send(self._active_ws, {"type": "shutdown", "reason": "server_stop"})
                await self._active_ws.close()
            except Exception:
                pass
            self._active_ws = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        logger.info("NexusBrain WS 服务已停止")

    async def send(self, ws: ServerConnection, data: dict):
        try:
            await ws.send(json.dumps(data, ensure_ascii=False))
        except ConnectionClosed:
            if ws is self._active_ws:
                self._active_ws = None

    async def send_config(self, ws: ServerConnection):
        await self.send(ws, {"type": "config", "data": self._config})

    async def broadcast_to_desktop(self, data: dict):
        """向桌宠推送消息（如果已连接）。"""
        if self._active_ws:
            await self.send(self._active_ws, data)

    async def send_to_satellite(self, platform: str, data: dict):
        """向指定平台卫星 Bot 发送消息。"""
        ws = self._satellite_ws.get(platform)
        if ws:
            await self.send(ws, data)

    async def _check_path(self, connection: ServerConnection, request):
        if request.path != self.path:
            return connection.respond(404, "Not Found")
        return None

    async def _handle_connection(self, ws: ServerConnection):
        """处理客户端连接 — 握手验证 → 区分 client_type → 消息循环。"""
        peer = ws.remote_address
        logger.info(f"客户端连接请求: {peer}")

        # ── 握手验证 ──
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            logger.warning(f"客户端握手超时: {peer}")
            await ws.close()
            return
        except (json.JSONDecodeError, ConnectionClosed):
            logger.warning(f"客户端握手无效: {peer}")
            await ws.close()
            return

        if msg.get("type") != "handshake" or msg.get("token") != self._token:
            logger.warning(f"客户端令牌不匹配: {peer}")
            try:
                await ws.send(json.dumps({
                    "type": "shutdown",
                    "reason": "instance_mismatch",
                }, ensure_ascii=False))
                await ws.close()
            except Exception:
                pass
            return

        client_type = msg.get("client_type", "desktop")

        # ── 桌宠连接（唯一）──
        if client_type == "desktop":
            if self._active_ws:
                logger.info("踢掉旧桌面端连接")
                try:
                    await self.send(self._active_ws, {
                        "type": "shutdown",
                        "reason": "new_instance",
                    })
                    await self._active_ws.close()
                except Exception:
                    pass

            self._active_ws = ws
            logger.info(f"桌宠已连接: {peer}")
            await self.send_config(ws)

        # ── 卫星 Bot 连接（多平台）──
        elif client_type == "satellite":
            platform = msg.get("platform", "unknown")
            # 踢掉同平台旧连接
            old = self._satellite_ws.get(platform)
            if old:
                try:
                    await self.send(old, {"type": "shutdown", "reason": "new_instance"})
                    await old.close()
                except Exception:
                    pass
            self._satellite_ws[platform] = ws
            logger.info(f"卫星 Bot [{platform}] 已连接: {peer}")
            await self.send(ws, {"type": "config", "data": self._config})

        else:
            logger.warning(f"未知 client_type: {client_type}")
            await ws.close()
            return

        # ── 消息循环 ──
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"收到非法 JSON: {raw[:100]}")
                    continue

                # 心跳响应：客户端发 ping → 服务端回 pong
                if msg.get("type") == "ping":
                    await self.send(ws, {"type": "pong"})
                    continue

                if self._handler:
                    await self._handler(msg, ws)

        except ConnectionClosed:
            logger.info(f"客户端已断开: {peer} (type={client_type})")
        finally:
            if client_type == "desktop" and self._active_ws is ws:
                self._active_ws = None
            elif client_type == "satellite":
                # 从 _satellite_ws 中移除匹配的 ws（value 匹配）
                to_remove = [p for p, w in self._satellite_ws.items() if w is ws]
                for p in to_remove:
                    del self._satellite_ws[p]
                    logger.info(f"卫星 Bot [{p}] 已移除")
