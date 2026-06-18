"""Nexus Brain 插件入口 — Hub 集中式大脑。

管理 WS 服务生命周期，对接 AstrBot Web Chat 和多平台卫星 Bot。
Hub 自身的 Web Chat 消息通过 AstrBot 原生管线处理。
桌面端和卫星 Bot 通过 WS 连接。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from astrbot.api import logger
from astrbot.api.star import Context, Star
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import LLMResponse

from .ws_server import NexusWSServer
from .brain import Brain
from .desktop_manager import DesktopManager

# logger 使用 astrbot.api.logger（已在文件头导入）
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_DESKTOP_MAIN = Path(__file__).parent / "desktop" / "main.py"
_RAG_CLEAN_RE = re.compile(r'<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>\s*', flags=re.DOTALL)


class NexusBrain(Star):
    """Nexus Hub 大脑插件。

    Hub 自身使用 AstrBot Web Chat 作为主人对话入口。
    桌面宠物通过 WS 连接接收表演指令。
    卫星 Bot (QQ/微信/飞书) 通过 WS 转发平台消息。
    """

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)

        if config is None:
            if _CONFIG_PATH.exists():
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            else:
                config = {}
        self.config = config

        ws_cfg = self.config.get("ws", {})
        self.ws = NexusWSServer(
            host=ws_cfg.get("host", "127.0.0.1"),
            port=ws_cfg.get("port", 8999),
            path=ws_cfg.get("path", "/nexus"),
        )
        self.ws.set_config(self.config)

        # ── 统一大脑 ──
        self.brain = Brain(context, self.config)

        char_cfg = self.config.get("character", {})
        self.char_name = char_cfg.get("name", "Nexus")

        # ── 防止递归 ──
        self._processing_desktop = False

        # ── 桌面 UI 子进程管理 ──
        ws_url = (
            f"ws://{ws_cfg.get('host', '127.0.0.1')}:"
            f"{ws_cfg.get('port', 8999)}{ws_cfg.get('path', '/nexus')}"
        )
        self.desktop_mgr = DesktopManager(_DESKTOP_MAIN, ws_url)
        self.ws.set_token(self.desktop_mgr.token)

        # ── 卫星 Bot 连接追踪 ──
        self._satellite_connections: dict[str, object] = {}  # platform → ws

        # ── Hub HTTP API（可选，仅当配置了 http_port 时启动）──
        hub_cfg = self.config.get("hub", {})
        self.hub_api = None
        if hub_cfg.get("http_port"):
            from .hub_api import HubAPI
            self.hub_api = HubAPI(
                host=hub_cfg.get("http_host", "127.0.0.1"),
                port=hub_cfg.get("http_port", 9000),
                on_message=self._handle_http_relay,
            )

    async def initialize(self):
        """启动 WS 服务 + 桌面 UI 子进程。"""
        self.ws.on_message(self._handle_ws_message)
        await self.ws.start()

        if self.config.get("desktop", {}).get("enabled", True):
            # 预检 PyQt5 可用性（AstrBot 更新后 venv 重建可能丢失）
            try:
                import PyQt5  # noqa: F401
            except ImportError:
                logger.error(
                    "⚠️ PyQt5 未安装，桌面宠物无法启动。"
                    "请检查插件根目录 requirements.txt 是否被 AstrBot 自动安装，"
                    "或手动执行: pip install PyQt5"
                )
            else:
                if self.desktop_mgr.spawn():
                    await self.desktop_mgr.health_check()
        else:
            logger.info("桌面 UI 已禁用 (desktop.enabled=false)")

        if self.hub_api:
            await self.hub_api.start()

        logger.info(f"NexusBrain Hub 初始化完成: character={self.char_name}")

    async def terminate(self):
        """停止 WS 服务 + 紧急保存记忆 + 终止桌面 UI。"""
        # Tier 3 紧急保存：在一切清理之前先把未同步的记忆写进小本本
        try:
            self.brain.emergency_save()
        except Exception as e:
            logger.warning(f"紧急记忆保存失败（非致命）: {e}")

        if self.hub_api:
            await self.hub_api.stop()
        self.desktop_mgr.kill()
        await self.ws.stop()
        logger.info("NexusBrain Hub 已终止")

    # ═══════════════════════════════════════════════════════════
    # Web Chat 消息通道（AstrBot 原生管线）
    # ═══════════════════════════════════════════════════════════

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """Hub Web Chat 消息入口。

        AstrBot Web Chat 发消息 → 拦截 → 注入统一 session → LLM 处理。
        注意：卫星 Bot 消息走 WS，不经过此方法。
        """
        if self._processing_desktop or self.brain._syncing:
            return

        # 提升为 admin 权限，确保 shell 执行等工具可用
        # （Web Chat 用户的 user_id 不是 QQ 号，可能不在 AstrBot admin 列表里）
        event.role = "admin"

        # 记录用户活动时间（用于空闲检测，决定何时触发 Tier 2 LLM 提取）
        self.brain.note_user_activity()

        # 更新上下文 token 上限（从 provider 配置读取）
        try:
            provider = self.context.get_using_provider()
            if provider:
                max_tokens = provider.provider_config.get("max_context_tokens", 0)
                if max_tokens > 0:
                    self.brain.update_context_limit(max_tokens)
        except Exception:
            pass  # 静默失败，使用默认值

        await self.brain.cache_provider_id(event)

        user_msg = getattr(req, "prompt", "") or ""
        if user_msg:
            self.brain.add_message("user", user_msg, source="webchat")
            clean_display = _RAG_CLEAN_RE.sub('', user_msg).strip()[:100]
            logger.info(f"[WebChat] 主人: {clean_display}")

        self.brain.inject_into_req(req)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """捕获 Web Chat 回复 → 写入 session → 推送表演指令到桌宠。"""
        if self._processing_desktop or self.brain._syncing:
            return

        reply_text = ""
        if hasattr(resp, "completion_text"):
            reply_text = resp.completion_text or ""

        if reply_text:
            self.brain.add_message("assistant", reply_text)
            logger.info(f"[安绪→WebChat]: {reply_text[:100]}")

        await self.brain.after_turn()

    # ═══════════════════════════════════════════════════════════
    # WS 消息路由 — 桌宠 + 卫星 Bot 共用
    # ═══════════════════════════════════════════════════════════

    async def _handle_ws_message(self, msg: dict, ws):
        """WS 消息分发：桌宠消息 vs 卫星 Bot 消息。"""
        msg_type = msg.get("type", "")

        if msg_type == "ping":
            await self.ws.send(ws, {"type": "pong"})

        elif msg_type == "config_request":
            await self.ws.send_config(ws)

        elif msg_type == "save_position":
            await self._save_mini_position(msg)

        elif msg_type == "user_input":
            # 桌宠还可以发消息（Layer 2 快捷输入，可选）
            await self._process_desktop_input(msg, ws)

        elif msg_type == "update_config":
            await self._handle_update_config(msg, ws)

        elif msg_type == "relay_message":
            # 卫星 Bot 转发消息
            await self._handle_satellite_message(msg, ws)

        else:
            logger.debug(f"未知 WS 消息类型: {msg_type}")

    async def _process_desktop_input(self, msg: dict, ws):
        """桌宠发送的用户消息（可选——Layer 2 的极简输入）。"""
        text = msg.get("text", "")
        if not text:
            return

        logger.info(f"[桌宠] 主人: {text[:100]}")
        self.brain.note_user_activity()
        self.brain.add_message("user", text, source="desktop")

        self._processing_desktop = True
        try:
            reply = await self.brain.generate(text)
        finally:
            self._processing_desktop = False

        self.brain.add_message("assistant", reply)
        logger.info(f"[安绪→桌宠]: {reply[:100]}")

        await self.brain.after_turn()

        # 返回回复
        await self.ws.send(ws, {
            "type": "text_chunk",
            "text": reply,
        })

    async def _handle_satellite_message(self, msg: dict, ws):
        """卫星 Bot 转发的平台消息 → Brain 处理 → 返回回复。"""
        platform = msg.get("platform", "unknown")
        text = msg.get("text", "")
        user_id = msg.get("user_id", "")
        request_id = msg.get("request_id", "")

        if not text:
            return

        logger.info(f"[{platform}] {user_id}: {text[:100]}")

        self.brain.note_user_activity()
        # 写入统一 session
        self.brain.add_message("user", text, source=platform)

        # 调用 LLM
        reply = await self.brain.generate(text)
        self.brain.add_message("assistant", reply)

        logger.info(f"[安绪→{platform}]: {reply[:100]}")

        await self.brain.after_turn()

        # 返回回复给卫星 Bot
        await self.ws.send(ws, {
            "type": "relay_reply",
            "request_id": request_id,
            "text": reply,
        })

    async def _handle_http_relay(self, platform: str, text: str, user_id: str) -> str:
        """HTTP 降级转发处理（非流式）。"""
        self.brain.note_user_activity()
        self.brain.add_message("user", text, source=platform)
        reply = await self.brain.generate(text)
        self.brain.add_message("assistant", reply)
        await self.brain.after_turn()
        return reply

    # ═══════════════════════════════════════════════════════════
    # 配置持久化
    # ═══════════════════════════════════════════════════════════

    async def _handle_update_config(self, msg: dict, ws):
        """桌面端个性化设置保存 → 写 config.yaml + 刷新 Brain。"""
        char_cfg = msg.get("character", {})
        mem_cfg = msg.get("memory", {})

        if char_cfg:
            new_name = char_cfg.get("name", "").strip()
            if new_name and new_name != self.char_name:
                self.char_name = new_name
                self.config.setdefault("character", {})["name"] = new_name
                self.brain.char_name = new_name
                logger.info(f"角色名已更新: {new_name}")

        if mem_cfg:
            folder_path = mem_cfg.get("folder_path", "")
            self.config.setdefault("memory", {})["folder_path"] = folder_path
            if folder_path:
                self.config["memory"]["enabled"] = True
                self.brain._memory_enabled = True
                self.brain._memory_folder = folder_path
                notebook_name = f"{self.char_name}的小本本.md"
                self.brain._memory_path = Path(self.brain._memory_folder) / notebook_name
                # 重新加载长期记忆
                self.brain._long_term_memory = self.brain._load_long_term_memory(
                    self.brain._memory_path
                )
                self.brain._system_prompt = self.brain._build_system_prompt()
                logger.info(
                    f"记忆已启用: {self.brain._memory_path} "
                    f"({len(self.brain._long_term_memory)} 字符)"
                )
            else:
                self.config["memory"]["enabled"] = False
                self.brain._memory_enabled = False
                self.brain._memory_path = None
                self.brain._long_term_memory = ""
                logger.info("记忆已关闭")

        # 持久化到 config.yaml
        try:
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(
                    self.config, f, allow_unicode=True,
                    default_flow_style=False, sort_keys=False,
                )
            logger.info("config.yaml 已更新")
        except OSError as e:
            logger.warning(f"写入 config.yaml 失败: {e}")

        # 回传更新后的配置给桌面端
        await self.ws.send_config(ws)
        """持久化迷你悬浮窗位置到 config.yaml。"""
        try:
            x, y = msg.get("x"), msg.get("y")
            if x is None or y is None:
                return
            self.config.setdefault("mini_window", {})["x"] = x
            self.config["mini_window"]["y"] = y
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            logger.info(f"迷你窗口位置已保存: x={x}, y={y}")
        except Exception as e:
            logger.warning(f"保存迷你窗口位置失败: {e}")
