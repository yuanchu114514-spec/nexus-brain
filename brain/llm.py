"""LLM 调用 — 桌面端 generate + QQ filter 钩子 + provider 管理。

从 brain.py 拆出：所有 LLM API 调用集中于此模块。
依赖 AstrBot context。
"""

from __future__ import annotations

import platform
from typing import Any

from astrbot.api import logger
from astrbot.api.provider import LLMResponse
from astrbot.core.agent.tool import ToolSet
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.message_type import MessageType
from astrbot.core.message.components import Plain
from astrbot.core.tools.computer_tools import (
    ExecuteShellTool,
    FileEditTool,
    FileReadTool,
    FileWriteTool,
    GrepTool,
    LocalPythonTool,
)
from astrbot.core.tools.message_tools import SendMessageToUserTool

DESKTOP_SESSION_ID = "desktop-nexus"


class LLMClient:
    """桌面端 + QQ filter 的 LLM 调用客户端。

    封装 tool_loop_agent 调用、provider 解析、工具集构建。
    """

    def __init__(self, context, session, persona):
        self.context = context
        self.session = session
        self.persona = persona
        self._provider_id: str = ""

    # ── 属性 ──

    @property
    def provider_id(self) -> str:
        return self._provider_id

    # ── 桌面端 LLM 调用 ──

    async def generate(self, user_message: str) -> str:
        """桌面端 LLM 调用 — 完整 tool_loop_agent 管线。

        等同于 QQ 端的 Agent 推理：System Prompt + 历史上下文 + 工具调用。
        """
        if not await self._resolve_provider_id():
            logger.error("LLMClient: provider_id 未设置，无法调用 LLM")
            return (
                "安绪的大脑还没连上喵~ "
                "请检查 AstrBot 是否已配置默认 LLM 提供商 (｡•́︿•̀｡)"
            )

        self._update_context_limit()

        tool_set = self._resolve_tools()
        event = self._build_event(user_message)
        system_prompt = self.persona.inject_local_env()
        contexts = self.session.get_contexts()[:-1]  # 不含当前消息

        try:
            response: LLMResponse = await self.context.tool_loop_agent(
                event=event,
                chat_provider_id=self._provider_id,
                prompt=user_message,
                system_prompt=system_prompt,
                contexts=contexts,
                tools=tool_set,
                max_steps=30,
                tool_call_timeout=120,
            )
            text = response.completion_text or ""
            if not text:
                logger.warning("LLM 返回空回复")
                return "嗯…安绪刚才走神了，能再说一遍吗喵~"
            return text
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return f"抱歉…安绪的大脑好像卡住了喵…({type(e).__name__})"

    # ── 记忆提取 LLM 调用 ──

    async def extract_memories(self, extraction_prompt: str,
                               system_prompt: str = "") -> str:
        """调用 LLM 提取记忆（供 MemoryManager 使用）。

        空工具集，纯文本提取，单步执行。
        """
        if not await self._resolve_provider_id():
            logger.warning("无法获取 provider_id，跳过记忆提取")
            return ""

        try:
            response: LLMResponse = await self.context.tool_loop_agent(
                event=self._build_event(extraction_prompt),
                chat_provider_id=self._provider_id,
                prompt=extraction_prompt,
                system_prompt=system_prompt or (
                    "你是一个记忆管理助手。你的任务是从对话中提取值得长期"
                    "保存的信息。只提取事实性内容，不要添加评论或情感表达。"
                ),
                contexts=[],
                tools=ToolSet(),
                max_steps=1,
                tool_call_timeout=30,
            )
            return (response.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"LLM 记忆提取调用失败: {e}")
            raise

    async def consolidate_memories(self, consolidate_prompt: str) -> str:
        """调用 LLM 整理记忆（供 MemoryManager 使用）。

        空工具集，纯文本整理，单步执行。
        """
        if not await self._resolve_provider_id():
            logger.warning("LLMClient: 无法获取 provider_id，跳过整理")
            return ""

        try:
            response: LLMResponse = await self.context.tool_loop_agent(
                event=self._build_event(consolidate_prompt),
                chat_provider_id=self._provider_id,
                prompt=consolidate_prompt,
                system_prompt=(
                    "你是一个记忆整理助手。你的任务是整理和合并记忆条目，"
                    "消除冗余，保留细节。只输出整理后的结果，不要添加额外解释。"
                ),
                contexts=[],
                tools=ToolSet(),
                max_steps=1,
                tool_call_timeout=60,
            )
            return (response.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"LLMClient 记忆整理调用失败: {e}")
            return ""

    # ── QQ filter 钩子 ──

    def inject_into_req(self, req) -> None:
        """向 QQ/WebChat LLM 请求注入 system_prompt 和统一 session contexts。"""
        contexts = self.session.get_contexts()
        if contexts:
            setattr(req, "contexts", contexts[:-1] if len(contexts) >= 1 else [])
        setattr(req, "system_prompt", self.persona.inject_local_env())

    async def cache_provider_id(self, event) -> None:
        """从 QQ/WebChat 消息事件缓存 provider_id（异步）。"""
        if isinstance(self._provider_id, str) and self._provider_id:
            return
        try:
            result = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            if isinstance(result, str) and result:
                self._provider_id = result
                logger.info(f"LLM provider 已缓存: {self._provider_id}")
        except Exception as e:
            logger.warning(f"获取 provider_id 失败: {e}")

    # ── 内部 ──

    async def _resolve_provider_id(self) -> bool:
        """获取或解析 provider_id。"""
        if self._provider_id:
            return True
        try:
            prov = self.context.get_using_provider()
            if prov:
                self._provider_id = prov.meta().id
                logger.info(f"LLM provider 从默认配置获取: {self._provider_id}")
                return True
        except Exception as e:
            logger.warning(f"从默认配置获取 provider 失败: {e}")
        return False

    def _resolve_tools(self) -> ToolSet:
        """获取完整工具集（内置工具 + 插件工具）。

        AstrBot 的 get_full_tool_set() 只返回插件注册工具，不返回内置工具。
        桌面路径不走 QQ filter 管线，需要手动补齐 7 个内置工具。
        """
        try:
            ftm = self.context.get_llm_tool_manager()
            tool_set = ftm.get_full_tool_set()
        except Exception as e:
            logger.warning(f"获取工具集失败: {e}")
            tool_set = ToolSet()

        try:
            tool_set.add_tool(ftm.get_builtin_tool(ExecuteShellTool))
            tool_set.add_tool(ftm.get_builtin_tool(LocalPythonTool))
            tool_set.add_tool(ftm.get_builtin_tool(FileReadTool))
            tool_set.add_tool(ftm.get_builtin_tool(FileWriteTool))
            tool_set.add_tool(ftm.get_builtin_tool(FileEditTool))
            tool_set.add_tool(ftm.get_builtin_tool(GrepTool))
            tool_set.add_tool(ftm.get_builtin_tool(SendMessageToUserTool))
            logger.debug(f"桌面端可用工具 ({len(tool_set.names())}): {tool_set.names()}")
        except Exception as e:
            logger.warning(f"补全内置工具失败: {e}")

        return tool_set

    def _build_event(self, user_message: str) -> AstrMessageEvent:
        """为桌面端消息构造最小 AstrMessageEvent。

        桌面端没有真实的 QQ 事件，因此构造虚拟事件以复用 Agent 管线。
        """
        bot_msg = AstrBotMessage()
        bot_msg.type = MessageType.FRIEND_MESSAGE
        bot_msg.self_id = "desktop-nexus"
        bot_msg.session_id = DESKTOP_SESSION_ID
        bot_msg.message_str = user_message
        bot_msg.message = [Plain(user_message)]
        bot_msg.sender = MessageMember(
            user_id="desktop-user", nickname=self.persona.master_name
        )

        platform_meta = PlatformMetadata(
            name="desktop-nexus",
            description="Nexus Desktop Client",
            id="desktop-nexus",
        )

        event = AstrMessageEvent(
            message_str=user_message,
            message_obj=bot_msg,
            platform_meta=platform_meta,
            session_id=DESKTOP_SESSION_ID,
        )
        event.role = "admin"
        return event

    def _update_context_limit(self) -> None:
        """从 provider 配置读取 max_context_tokens。"""
        try:
            prov = self.context.get_using_provider()
            if prov:
                max_tokens = prov.provider_config.get("max_context_tokens", 0)
                if max_tokens > 0:
                    self.context_token_limit = max_tokens
        except Exception:
            pass
