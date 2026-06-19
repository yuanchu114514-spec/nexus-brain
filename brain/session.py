"""会话管理 — 消息存储、持久化、RAG 清洗。

从 brain.py 拆出：所有消息队列操作集中于此模块。
不依赖 AstrBot context。
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger

MAX_TURNS = 80
SESSION_FILE = Path(__file__).resolve().parent.parent / "session.json"
DESKTOP_SESSION_ID = "desktop-nexus"

# RAG 注入标记正则（livingmemory 等插件会在用户消息前注入记忆块）
_RAG_CLEAN_RE = re.compile(
    r'<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>\s*', flags=re.DOTALL
)
# RAG 内容提取正则（用于日志追溯，不丢弃 RAG 信息）
_RAG_PARSE_RE = re.compile(
    r'<RAG-Faiss-Memory>(.*?)</RAG-Faiss-Memory>', flags=re.DOTALL
)


class SessionManager:
    """统一对话会话管理。

    所有平台（Web Chat / 桌宠 / 卫星 Bot）的消息写入同一个 session，
    LLM 看到的上下文是完整的跨平台对话历史。
    """

    def __init__(self):
        self._messages: deque[dict[str, Any]] = deque(maxlen=MAX_TURNS * 2)
        self._total_user_turns: int = 0
        self._load()

    # ── 属性 ──

    @property
    def turn_count(self) -> int:
        """绝对轮次计数（永不因 deque 截断倒退）。"""
        return self._total_user_turns

    @property
    def messages(self) -> deque[dict[str, Any]]:
        return self._messages

    # ── 消息操作 ──

    def add_message(self, role: str, content: str, source: str = "") -> None:
        """添加消息到统一 session。

        同时做 RAG 标签日志追溯（不丢弃信息），以及清洗 RAG 标记。
        """
        if role == "user":
            # 解析 RAG 注入（不丢弃，记录到日志用于调试）
            rag_matches = _RAG_PARSE_RE.findall(content)
            if rag_matches:
                logger.debug(f"RAG 注入: {len(rag_matches)} 条记忆")
            content = _RAG_CLEAN_RE.sub('', content).strip()
            if not content:
                return

        turn = self._total_user_turns if role == "user" else self._total_user_turns
        self._messages.append({
            "role": role,
            "content": content,
            "source": source,
            "turn": turn,
        })

        if role == "user":
            self._total_user_turns += 1

    def get_contexts(self) -> list[dict[str, str]]:
        """获取 LLM 上下文格式的消息列表。"""
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self._messages
        ]

    def clear(self) -> None:
        """清空会话（不持久化）。"""
        self._messages.clear()
        self._total_user_turns = 0

    # ── 消息迭代（供记忆系统使用）──

    def unsynced_messages(self, last_synced_turn: int) -> list[dict[str, Any]]:
        """获取自 last_synced_turn 以来的未同步消息。"""
        return [m for m in self._messages if m.get("turn", 0) > last_synced_turn]

    def uncheckpointed_messages(self, last_checkpoint_turn: int) -> list[dict[str, Any]]:
        """获取自 last_checkpoint_turn 以来的未做检查点的消息。"""
        return [m for m in self._messages if m.get("turn", 0) > last_checkpoint_turn]

    # ── 持久化 ──

    def save(self) -> None:
        """保存 session 到 JSON 文件。

        保存消息列表 + 绝对轮次计数器，插件重载不丢记忆。
        """
        try:
            data = {
                "messages": list(self._messages),
                "total_user_turns": self._total_user_turns,
            }
            SESSION_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug(f"Session 已保存: {len(self._messages)} 条消息, {self._total_user_turns} 轮")
        except OSError as e:
            logger.warning(f"保存 session 失败: {e}")

    def _load(self) -> None:
        """从 JSON 文件恢复 session。"""
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            msgs = data.get("messages", [])
            if msgs:
                self._messages = deque(msgs, maxlen=MAX_TURNS * 2)
                self._total_user_turns = data.get("total_user_turns", 0)
                logger.info(
                    f"Session 已恢复: {len(self._messages)} 条消息, "
                    f"{self._total_user_turns} 轮"
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"恢复 session 失败，从空白开始: {e}")
