"""统一大脑模块 — 安绪的 AI 核心（协调器）。

将原 1650 行 brain.py 拆分为 6 个子模块：
  session  — 消息存储 + 持久化
  persona  — 人格 System Prompt
  notebook — 小本本文件 I/O
  memory   — 三层安全网记忆编排
  llm      — LLM API 调用

Brain 协调器只做 wiring，业务逻辑全部在子模块中。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

from .session import SessionManager, MAX_TURNS, SESSION_FILE, DESKTOP_SESSION_ID
from .persona import SystemPrompt, DEFAULT_CHAR_NAME, DEFAULT_MASTER_NAME
from .notebook import NotebookIO
from .memory import MemoryManager
from .llm import LLMClient


class Brain:
    """Nexus 统一 AI 大脑（协调器）。

    管理跨窗口对话 session、人格 system prompt、LLM 调用。
    所有平台和桌面端共用同一个 Brain 实例。

    向后兼容：公开 API 与旧 brain.py 完全一致。
    """

    def __init__(self, context, config: dict):
        self.context = context
        self._config = config

        char_cfg = config.get("character", {})
        self.char_name = char_cfg.get("name", DEFAULT_CHAR_NAME)
        self.master_name = char_cfg.get("master_name", DEFAULT_MASTER_NAME)

        # ── 记忆系统参数化 ──
        mem_cfg = config.get("memory", {})
        self._memory_enabled = mem_cfg.get("enabled", False)
        self._memory_folder = mem_cfg.get("folder_path", "")
        if self._memory_enabled and self._memory_folder:
            notebook_name = f"{self.char_name}的小本本.md"
            self._memory_path = Path(self._memory_folder) / notebook_name
        else:
            self._memory_path = None

        # ── 子模块: 自底向上初始化 ──
        # 1. 小本本 I/O（纯文件层）
        self._notebook = NotebookIO(
            memory_enabled=self._memory_enabled,
            memory_path=self._memory_path,
            char_name=self.char_name,
        )

        # 2. 会话管理
        self._session = SessionManager()

        # 3. 人格 System Prompt（含长期记忆）
        long_term_memory = self._notebook.load_memory()
        self._persona = SystemPrompt(
            char_name=self.char_name,
            master_name=self.master_name,
            long_term_memory=long_term_memory,
        )
        self._system_prompt = self._persona.build()

        # 4. LLM 客户端
        self._llm = LLMClient(context, self._session, self._persona)

        # 5. 记忆管理器（依赖以上全部）
        self._memory = MemoryManager(
            self._session, self._notebook, self._persona, self._llm
        )

        # ── 向后兼容: _long_term_memory 属性 ──
        self._provider_id = ""  # 由 LLMClient 管理，保留引用兼容

    # ═══════════════════════════════════════════════════════════
    # 公开属性（向后兼容）
    # ═══════════════════════════════════════════════════════════

    @property
    def _long_term_memory(self) -> str:
        return self._persona.long_term_memory

    @_long_term_memory.setter
    def _long_term_memory(self, value: str) -> None:
        self._persona.long_term_memory = value

    @property
    def _syncing(self) -> bool:
        return self._memory.syncing

    @_syncing.setter
    def _syncing(self, value: bool) -> None:
        self._memory._syncing = value

    # ═══════════════════════════════════════════════════════════
    # Session API
    # ═══════════════════════════════════════════════════════════

    def add_message(self, role: str, content: str, source: str = "") -> None:
        self._session.add_message(role, content, source)

    def get_contexts(self) -> list[dict[str, str]]:
        return self._session.get_contexts()

    @property
    def turn_count(self) -> int:
        return self._session.turn_count

    def clear(self) -> None:
        self._session.clear()

    # ═══════════════════════════════════════════════════════════
    # System Prompt API
    # ═══════════════════════════════════════════════════════════

    @property
    def system_prompt(self) -> str:
        return self._persona._cache or self._persona.build()

    def _build_system_prompt(self) -> str:
        self._system_prompt = self._persona.build()
        return self._system_prompt

    def _load_long_term_memory(self, notebook_path: Path | None = None) -> str:
        """向后兼容：重新加载长期记忆。"""
        if notebook_path is not None:
            self._notebook.path = notebook_path
        return self._notebook.load_memory()

    # ═══════════════════════════════════════════════════════════
    # Memory API
    # ═══════════════════════════════════════════════════════════

    def note_user_activity(self) -> None:
        self._memory.note_user_activity()

    def update_context_limit(self, max_tokens: int) -> None:
        self._memory.update_context_limit(max_tokens)

    def emergency_save(self) -> None:
        self._memory.emergency_save()

    async def after_turn(self) -> None:
        refreshed = await self._memory.after_turn()
        if refreshed:
            self._long_term_memory = self._notebook.load_memory()
            self._system_prompt = self._persona.build()

    # ═══════════════════════════════════════════════════════════
    # LLM API
    # ═══════════════════════════════════════════════════════════

    async def generate(self, user_message: str) -> str:
        return await self._llm.generate(user_message)

    def inject_into_req(self, req) -> None:
        self._llm.inject_into_req(req)

    async def cache_provider_id(self, event) -> None:
        await self._llm.cache_provider_id(event)
