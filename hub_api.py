"""Hub HTTP API — 辅助端点（健康检查 + WS 降级转发）。

主通道是 WS，HTTP 仅用于：
- 卫星 Bot 初始化时验证 Hub 可达性
- WS 断线时的降级同步转发
"""

from __future__ import annotations

import json
from aiohttp import web
from astrbot.api import logger


class HubAPI:
    """Hub HTTP API 服务。轻量辅助，不承载主要消息流。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9000,
                 on_message: callable = None):
        self.host = host
        self.port = port
        self._on_message = on_message
        self._app = web.Application()
        self._runner: web.AppRunner | None = None

        # 注册路由
        self._app.router.add_get("/api/health", self._health)
        self._app.router.add_post("/api/message", self._relay_message)

    async def start(self):
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Hub HTTP API 已启动: http://{self.host}:{self.port}")

    async def stop(self):
        if self._runner:
            await self._runner.cleanup()
            logger.info("Hub HTTP API 已停止")

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "version": "0.5.0",
            "satellites": list(getattr(self, '_satellites', {}).keys()),
        })

    async def _relay_message(self, request: web.Request) -> web.Response:
        """WS 降级通道：HTTP POST 转发消息（非流式）。"""
        if not self._on_message:
            return web.json_response({"error": "no handler"}, status=503)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "invalid json"}, status=400)

        platform = body.get("platform", "unknown")
        text = body.get("text", "")
        user_id = body.get("user_id", "")

        if not text:
            return web.json_response({"error": "missing text"}, status=400)

        try:
            reply = await self._on_message(platform, text, user_id)
            return web.json_response({"reply": reply})
        except Exception as e:
            logger.error(f"HTTP 转发处理失败: {e}")
            return web.json_response({"error": str(e)}, status=500)
