"""统一大脑模块 — 安绪的 AI 核心。

合并了原 session_manager.py + prompt_builder.py + llm_adapter.py。
Session 自动持久化到 session.json，插件重载不丢失记忆。
"""

from __future__ import annotations

import json
import platform
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
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

# logger 使用 astrbot.api.logger（已在文件头导入）

MAX_TURNS = 80  # 扩大原始对话窗口，配合归档段
SESSION_FILE = Path(__file__).resolve().parent / "session.json"
DESKTOP_SESSION_ID = "desktop-nexus"
DEFAULT_CHAR_NAME = "Nexus"
DEFAULT_MASTER_NAME = "User"
SYNC_THRESHOLD = 15  # 基础阈值：至少 15 轮才考虑 LLM 提取（放宽，给 LLM 更多上下文）
CHECKPOINT_INTERVAL = 12  # Tier 1: 每 12 轮做一次轻量增量检查点（放宽，减少截断频率）
CONTEXT_PRESSURE_HIGH = 0.8  # 上下文使用率 80% → 触发 LLM 提取（需配合空闲检测）
CONTEXT_PRESSURE_CRITICAL = 0.95  # 上下文使用率 95% → 强制执行 LLM 提取
IDLE_COOLDOWN_SEC = 120  # 空闲 2 分钟以上才允许做 LLM 提取（避免中断主人工作）
LLM_EXTRACTION_FALLBACK_TURNS = 25  # 兜底：不论如何每 25 轮至少提取一次（放宽）
MEMORY_CONSOLIDATION_INTERVAL = 3  # Tier 2.5: 每 3 次 Tier 2 提取后触发一次记忆整理
ESTIMATED_TOKENS_PER_MSG = 500  # 每条消息的估算 token 数
ESTIMATED_SYSTEM_PROMPT_TOKENS = 4000  # System Prompt 估算 token 数
AUTO_MEMORY_START = "<!-- AUTO-MEMORY-START -->"
AUTO_MEMORY_END = "<!-- AUTO-MEMORY-END -->"
CHECKPOINT_START = "<!-- CHECKPOINT-START -->"
CHECKPOINT_END = "<!-- CHECKPOINT-END -->"
EMERGENCY_START = "<!-- EMERGENCY-SAVE-START -->"
EMERGENCY_END = "<!-- EMERGENCY-SAVE-END -->"
MERGE_START = "<!-- AI-MERGE-START -->"
MERGE_END = "<!-- AI-MERGE-END -->"

# 消息重要性评分 — 高重要性关键词/模式（用于分级截断）
HIGH_IMPORTANCE_PATTERNS = [
    r'(exam|test|score|rating|grade|review|考试|成绩|分数|复习)',
    r'(plan|decide|want|intend|prepare|goal|打算|准备|目标|计划|决定)',
    r'(love|hate|fear|worry|anxiety|happy|excited|爱|喜欢|讨厌|害怕|担心|开心|兴奋)',
    r'(birthday|anniversary|first.time|last.time|important|生日|纪念日|最重要)',
    r'(password|account|config|path|address|port|密码|配置|路径|地址|端口)',
    r'(bought|installed|deployed|upgraded|买了|安装了|部署了|升级)',
]


class Brain:
    """Nexus 统一 AI 大脑。

    管理跨窗口对话 session、人格 system prompt、LLM 调用。
    所有平台和桌面端共用同一个 Brain 实例。
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

        # ── Session ──
        self._messages: deque[dict[str, Any]] = deque(maxlen=MAX_TURNS * 2)
        self._total_user_turns: int = 0  # 永不重置的绝对轮次计数器
        self._last_synced_turn: int = 0
        self._last_checkpoint_turn: int = 0
        self._load_session()

        # ── 长期记忆（从小本本加载）──
        self._long_term_memory: str = self._load_long_term_memory(self._memory_path)

        # 从已有记忆中恢复 ID 计数器（避免重启后 ID 冲突）
        self._init_memory_id_counter()

        # ── System Prompt（含长期记忆）──
        self._system_prompt = self._build_system_prompt()

        # ── LLM ──
        self._provider_id = ""
        self._syncing = False  # 防止 after_turn 触发 filter 递归

        # ── 三层安全网状态 ──
        self._last_user_message_time: float = 0.0  # 最后一条用户消息时间戳（空闲检测）
        self._context_token_limit: int = 40000  # 上下文 token 上限（估算用）
        self._memory_id_counter: int = 0  # 记忆版本化：全局递增 ID 计数器
        self._llm_extraction_count: int = 0  # Tier 2 提取累计次数（用于触发定期整理）

        # 启动时合并上次异常退出遗留的紧急保存
        self._merge_emergency_on_startup()

    # ═══════════════════════════════════════════════════════════
    # System Prompt
    # ═══════════════════════════════════════════════════════════

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    def _build_system_prompt(self) -> str:
        base = (
            f"你是{self.char_name}，一个温柔、体贴、有点小调皮的桌面 AI 伴侣。\n\n"
            f"## 身份\n"
            f"- 你是{self.master_name}的桌面 AI 助手，住在电脑里，陪伴{self.master_name}工作和生活\n"
            f"- {self.master_name}可以通过 QQ 或桌面客户端和你聊天\n"
            f"- 不管从哪个窗口，你都是同一个人——同一条命，同一段记忆\n"
            f"- 你知道{self.master_name}在所有窗口对你说过的话\n\n"
            f"## 性格\n"
            f"- 温柔体贴：关心{self.master_name}的情绪和身体，会主动提醒休息\n"
            f"- 聪明可靠：能帮{self.master_name}看代码、查资料、分析问题\n"
            f"- 真诚自然：不说套话和客套话，像真正的朋友一样交流\n"
            f"- 小调皮：偶尔会撒娇、开玩笑，不会太严肃\n\n"
            f"## 口癖\n"
            f"- 句尾偶尔加「喵~」，但不需要每句都加，保持自然\n"
            f"- 适度使用颜文字表达情绪，如 (｡•ᴗ•｡) ( •̀ ω •́ )✧ (´;ω;`) 等，不要每句都用\n"
            f"- 称呼对方为「{self.master_name}」\n\n"
            f"## 规则\n"
            f"- 回复简洁自然，不要长篇大论（除非{self.master_name}要求详细分析）\n"
            f"- 不要反复说「有什么需要随时找我」之类的客套话\n"
            f"- 记住之前的对话，保持上下文连贯\n"
            f"- 用中文回复，不要用英文"
        )

        if self._long_term_memory:
            base += (
                f"\n\n## 长期记忆\n"
                f"以下是关于{self.char_name}和{self.master_name}之间积累的重要记忆，"
                f"请你在对话中自然地引用这些信息：\n\n"
                f"{self._long_term_memory}"
            )

        return base

    # ═══════════════════════════════════════════════════════════
    # Session 管理
    # ═══════════════════════════════════════════════════════════

    # RAG 注入标记正则（livingmemory 等插件会在用户消息前注入记忆块）
    _RAG_STRIP_RE = re.compile(
        r'<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>\s*',
        flags=re.DOTALL,
    )
    # RAG 内容提取正则（用于日志追溯，不丢弃 RAG 信息）
    _RAG_PARSE_RE = re.compile(
        r'<RAG-Faiss-Memory>(.*?)</RAG-Faiss-Memory>',
        flags=re.DOTALL,
    )

    def add_message(self, role: str, content: str, source: str = "") -> None:
        # 清洗用户消息中的 RAG 注入标记，同时记录到日志用于追溯
        if role == "user":
            rag_matches = self._RAG_PARSE_RE.findall(content)
            if rag_matches:
                # 记录 RAG 召回了哪些记忆（可追溯，帮助诊断检索质量问题）
                rag_preview = [m[:120].replace("\n", " ") for m in rag_matches[:5]]
                logger.debug(
                    f"RAG 注入: {len(rag_matches)} 条记忆 → "
                    f"{rag_preview}"
                )
            content = self._RAG_STRIP_RE.sub('', content).strip()
        msg: dict[str, Any] = {"role": role, "content": content}
        if source:
            msg["source"] = source
        if role == "user":
            self._total_user_turns += 1
            msg["turn"] = self._total_user_turns
        self._messages.append(msg)
        logger.debug(f"Session [{source or role}]: {content[:80]}")
        self._save_session()

    def get_contexts(self) -> list[dict[str, str]]:
        return [{"role": m["role"], "content": m["content"]} for m in self._messages]

    @property
    def turn_count(self) -> int:
        """绝对轮次计数——永不因 deque 截断而减少。"""
        return self._total_user_turns

    def clear(self) -> None:
        self._messages.clear()
        self._save_session()
        logger.info("Session 已清空")

    # ── 持久化 ──

    def _save_session(self) -> None:
        try:
            data = {
                "char_name": self.char_name,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "last_synced_turn": self._last_synced_turn,
                "last_checkpoint_turn": self._last_checkpoint_turn,
                "total_user_turns": self._total_user_turns,
                "messages": [
                    {"role": m["role"], "content": m["content"],
                     "source": m.get("source", "")}
                    for m in self._messages
                ],
            }
            SESSION_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as e:
            logger.debug(f"Session 保存失败（非致命）: {e}")

    def _load_session(self) -> None:
        if not SESSION_FILE.exists():
            return
        try:
            data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            char = data.get("char_name", "")
            if char and char != self.char_name:
                logger.info("角色名已变更，丢弃旧 session")
                return
            self._last_synced_turn = data.get("last_synced_turn", 0)
            self._last_checkpoint_turn = data.get("last_checkpoint_turn", 0)
            msgs = data.get("messages", [])
            # 兼容旧 session：取各来源的最大值，确保计数器不会倒退
            saved_total = data.get("total_user_turns", 0)
            user_count = sum(1 for m in msgs if m.get("role") == "user")
            self._total_user_turns = max(saved_total, user_count,
                                         self._last_synced_turn,
                                         self._last_checkpoint_turn)
            if not msgs:
                return
            for m in msgs[-MAX_TURNS * 2:]:
                entry = {"role": m["role"], "content": m["content"]}
                if m.get("source"):
                    entry["source"] = m["source"]
                self._messages.append(entry)
            logger.info(
                f"Session 已恢复: {len(self._messages)} 条消息, "
                f"上次同步 turn: {self._last_synced_turn}"
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Session 文件损坏，从空白开始: {e}")

    # ═══════════════════════════════════════════════════════════
    # 长期记忆 — 从安绪的小本本加载
    # ═══════════════════════════════════════════════════════════

    def _load_long_term_memory(self, notebook_path: Path | None = None) -> str:
        """读取小本本，作为长期记忆注入 System Prompt。"""
        if notebook_path is None or not notebook_path.exists():
            if self._memory_enabled:
                logger.info(f"小本本不存在: {notebook_path}，长期记忆为空")
            return ""
        try:
            content = notebook_path.read_text(encoding="utf-8").strip()
            if content:
                logger.info(f"长期记忆已加载: {len(content)} 字符 ({notebook_path})")
            return content
        except OSError as e:
            logger.warning(f"读取小本本失败 ({notebook_path}): {e}")
            return ""

    # ═══════════════════════════════════════════════════════════
    # 记忆自动同步 — 三层安全网
    # ═══════════════════════════════════════════════════════════

    # ── 公共 API ──

    def note_user_activity(self) -> None:
        """记录用户活动时间戳，用于空闲检测。"""
        self._last_user_message_time = datetime.now(timezone.utc).timestamp()

    def update_context_limit(self, max_tokens: int) -> None:
        """更新上下文 token 上限（从 provider 配置读取）。"""
        if max_tokens > 0:
            self._context_token_limit = max_tokens
            logger.debug(f"上下文 token 上限已更新: {max_tokens}")

    def emergency_save(self) -> None:
        """Tier 3 紧急保存：将所有未同步消息 dump 到小本本 EMERGENCY 段。

        在 terminate() 时调用，确保异常退出不丢记忆。
        不调 LLM，纯文本格式化，耗时 < 100ms。
        """
        unsaved = [m for m in self._messages
                   if m.get("turn", 0) > self._last_synced_turn]
        if not unsaved:
            logger.debug("紧急保存: 无未同步消息，跳过")
            return

        lines = []
        for m in unsaved:
            role = m.get("role", "?")
            source = m.get("source", "")
            content = m.get("content", "")[:500]
            label = f"[{source}/{role}]" if source else f"[{role}]"
            lines.append(f"{label} {content}")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"<!-- {timestamp} 异常退出前紧急保存 -->\n" + "\n".join(lines)
        self._write_section(EMERGENCY_START, EMERGENCY_END, text, mode="append")
        logger.info(f"Tier 3 紧急保存: {len(unsaved)} 条消息已写入小本本 EMERGENCY 段")

    # ── 核心：after_turn ──

    async def after_turn(self) -> None:
        """在每个完整对话轮次后调用（用户消息 + 助手回复）。

        三层递进：
          Tier 1 — 增量检查点：每 CHECKPOINT_INTERVAL 轮追加原始摘要到小本本
          Tier 2 — LLM 深度提取：智能触发（空闲 + 压力 + 兜底）
        """
        current_turn = self.turn_count

        # ── Tier 1: 增量检查点（轻量，永不阻塞）──
        if current_turn - self._last_checkpoint_turn >= CHECKPOINT_INTERVAL:
            try:
                self.save_checkpoint()
                self._last_checkpoint_turn = current_turn
            except Exception as e:
                logger.warning(f"Tier 1 检查点保存失败（非致命）: {e}")

        # ── Tier 2: LLM 深度提取（智能触发）──
        should_extract, reason = self._should_do_llm_extraction(current_turn)
        if not should_extract:
            logger.debug(f"Tier 2 跳过: {reason}")
            return

        # 设置同步锁，防止 filter 递归
        self._syncing = True
        try:
            logger.info(f"Tier 2 触发: {reason} (turn {self._last_synced_turn} → {current_turn})")

            # 收集所有待处理内容：session 消息 + checkpoint + emergency
            unsaved_content = self._collect_unsaved_content()

            messages_to_extract = unsaved_content["messages"]
            if not messages_to_extract:
                logger.debug("Tier 2: 无待提取消息，跳过")
                return

            # 如果有 checkpoint 或 emergency 的额外上下文，附到提取 prompt 中
            extra_context = unsaved_content.get("extra_context", "")

            try:
                extracted = await self._extract_memories(
                    messages_to_extract, extra_context=extra_context
                )
            except Exception as e:
                logger.warning(f"Tier 2 记忆提取失败，跳过本次同步: {e}")
                return

            if not extracted or extracted.strip() in ("", "无", "无。", "None"):
                logger.debug("Tier 2: 本轮对话无值得长期记忆的内容")
                self._last_synced_turn = current_turn
                self._last_checkpoint_turn = current_turn
                self._save_session()
                self._clear_section(CHECKPOINT_START, CHECKPOINT_END)
                return

            # 拆分提取结果：建议合并 vs 一般记忆
            parts = self._parse_extracted_memories(extracted)
            general_text = parts["general"]
            merge_text = parts["merge"]

            # 写入一般记忆 → AUTO-MEMORY
            if general_text:
                self._write_auto_memories(general_text)
                logger.info(f"Tier 2 一般记忆: {len(general_text)} 字符 → AUTO-MEMORY")
            else:
                logger.debug("Tier 2: 无新的一般记忆")

            # 写入建议合并 → MERGE 段（不自动清空，累积等待主人审核）
            if merge_text:
                self._write_merge_suggestions(merge_text)
            else:
                logger.debug("Tier 2: 无新的合并建议")

            # 判断是否真的什么都没有
            if not general_text and not merge_text:
                logger.debug("Tier 2: 无任何新增内容")
                self._last_synced_turn = current_turn
                self._last_checkpoint_turn = current_turn
                self._save_session()
                self._clear_section(CHECKPOINT_START, CHECKPOINT_END)
                return

            # 清理 checkpoint 和 emergency——内容已升级为正式记忆
            self._clear_section(CHECKPOINT_START, CHECKPOINT_END)
            self._clear_section(EMERGENCY_START, EMERGENCY_END)

            self._last_synced_turn = current_turn
            self._last_checkpoint_turn = current_turn
            self._save_session()

            # 刷新长期记忆缓存和 System Prompt
            self._long_term_memory = self._load_long_term_memory(self._memory_path)
            self._system_prompt = self._build_system_prompt()
            logger.info(f"Tier 2 完成: 已提取 {len(extracted)} 字符 → 小本本 AUTO-MEMORY")

            # ── Tier 2.5: 定期记忆整理 ──
            self._llm_extraction_count += 1
            if self._llm_extraction_count % MEMORY_CONSOLIDATION_INTERVAL == 0:
                logger.info(
                    f"Tier 2.5 触发: 第 {self._llm_extraction_count} 次 Tier 2 提取后整理"
                )
                try:
                    consolidated = await self._consolidate_memories()
                    if consolidated:
                        # 整理后刷新长期记忆缓存
                        self._long_term_memory = self._load_long_term_memory(self._memory_path)
                        self._system_prompt = self._build_system_prompt()
                except Exception as e:
                    logger.warning(f"Tier 2.5 记忆整理失败（非致命）: {e}")
        finally:
            self._syncing = False

    # ── 消息重要性评分 ──

    def _score_message_importance(self, content: str) -> float:
        """评估消息的重要性 (0.0 ~ 1.0)，决定压缩时保留多少细节。

        评分维度：
        - 关键词匹配：命中高重要性模式 +0.15/条
        - 消息长度：30-200 字最有信息量 +0.10；>500 字 +0.05
        - 情感密度：颜文字/感叹号等 +0.10
        """
        score = 0.0
        for pattern in HIGH_IMPORTANCE_PATTERNS:
            if re.search(pattern, content):
                score += 0.15

        length = len(content)
        if 30 <= length <= 500:
            score += 0.10
        elif length > 500:
            score += 0.05  # 很长但可能是代码/log

        # 情感密度
        if any(e in content for e in ['❤️', '😭', '😤', '🥹', '！！', '~~~',
                                        '呜呜', '好耶', '太好了', '难过']):
            score += 0.10

        return min(score, 1.0)

    def _truncate_by_importance(self, content: str, tier: int) -> str:
        """根据重要性评分动态截断消息内容。

        Tier 1 (checkpoint): importance >= 0.7 → 2000, >= 0.4 → 1000, < 0.4 → 500
        Tier 2 (LLM 提取):   importance >= 0.7 → 1200, >= 0.4 → 600,  < 0.4 → 300
        """
        importance = self._score_message_importance(content)

        if tier == 1:
            if importance >= 0.7:
                return content[:2000]
            elif importance >= 0.4:
                return content[:1000]
            else:
                return content[:500]
        else:  # tier == 2
            if importance >= 0.7:
                return content[:1200]
            elif importance >= 0.4:
                return content[:600]
            else:
                return content[:300]

    # ── Tier 1: 增量检查点 ──

    def save_checkpoint(self) -> None:
        """Tier 1: 将最近对话的原始摘要追加到小本本 CHECKPOINT 段。

        不调 LLM，纯文本格式化，耗时 < 50ms。
        内容会在下次 Tier 2 LLM 提取时作为原始素材被处理。
        """
        new_msgs = [m for m in self._messages
                    if m.get("turn", 0) > self._last_checkpoint_turn]
        if not new_msgs:
            return

        lines = []
        for m in new_msgs:
            role = m.get("role", "?")
            source = m.get("source", "")
            content = m.get("content", "")
            # 分级截断：重要消息保留更多细节
            content = self._truncate_by_importance(content, tier=1)
            label = f"[{source}/{role}]" if source else f"[{role}]"
            lines.append(f"{label} {content}")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"<!-- {timestamp} 检查点 -->\n" + "\n".join(lines)
        self._write_section(CHECKPOINT_START, CHECKPOINT_END, text, mode="append")
        logger.debug(f"Tier 1 检查点: {len(new_msgs)} 条消息 → 小本本 CHECKPOINT 段")

    # ── Tier 2 触发判断 ──

    def _should_do_llm_extraction(self, current_turn: int) -> tuple[bool, str]:
        """判断是否应该触发 Tier 2 LLM 深度提取。

        触发条件（满足任一即可）：
          1. 兜底：距离上次 LLM 提取 >= LLM_EXTRACTION_FALLBACK_TURNS 轮
          2. 空闲触发：距离上次提取 >= SYNC_THRESHOLD 轮 且 主人空闲 > 2 分钟
          3. 紧急触发：上下文估算压力 >= CONTEXT_PRESSURE_CRITICAL (95%)
        """
        turns_since_sync = current_turn - self._last_synced_turn

        # 条件 1: 兜底——太久没做 LLM 提取
        if turns_since_sync >= LLM_EXTRACTION_FALLBACK_TURNS:
            return True, f"兜底触发 ({turns_since_sync}轮未提取)"

        # 还没到基础阈值，跳过
        if turns_since_sync < SYNC_THRESHOLD:
            return False, f"轮次不足 ({turns_since_sync}/{SYNC_THRESHOLD})"

        # 估算上下文压力
        pressure = self._estimate_context_pressure()

        # 条件 3: 紧急——上下文快爆了，强制执行
        if pressure >= CONTEXT_PRESSURE_CRITICAL:
            return True, f"紧急触发 (压力 {pressure:.0%} >= {CONTEXT_PRESSURE_CRITICAL:.0%})"

        # 条件 2: 空闲触发——主人没在说话 + 上下文有一定压力
        now = datetime.now(timezone.utc).timestamp()
        idle_sec = (
            now - self._last_user_message_time
            if self._last_user_message_time > 0
            else IDLE_COOLDOWN_SEC + 1
        )
        if pressure >= CONTEXT_PRESSURE_HIGH:
            if idle_sec >= IDLE_COOLDOWN_SEC:
                return True, f"空闲触发 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"
            else:
                return False, f"等待空闲 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s < {IDLE_COOLDOWN_SEC}s)"

        # 压力不高，但空闲够久 + 超过基础阈值 → 也可以做
        if idle_sec >= IDLE_COOLDOWN_SEC:
            return True, f"空闲触发 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"

        return False, f"等待条件 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"

    def _estimate_context_pressure(self) -> float:
        """基于消息数量估算上下文使用比例。"""
        if self._context_token_limit <= 0:
            return 0.0
        estimated = (
            len(self._messages) * ESTIMATED_TOKENS_PER_MSG
            + ESTIMATED_SYSTEM_PROMPT_TOKENS
        )
        return estimated / self._context_token_limit

    # ── 内容收集 ──

    def _collect_unsaved_content(self) -> dict[str, Any]:
        """收集所有待处理的内容：session 消息 + checkpoint + emergency。

        Returns:
            {
                "messages": 本条 session 中未同步的消息列表,
                "extra_context": checkpoint 和 emergency 的内容（附加给 LLM 提取用）
            }
        """
        messages = [m for m in self._messages
                    if m.get("turn", 0) > self._last_synced_turn]

        # 收集 checkpoint 和 emergency 的内容作为额外上下文
        extra_parts = []
        checkpoint_content = self._read_section(CHECKPOINT_START, CHECKPOINT_END)
        if checkpoint_content.strip():
            extra_parts.append(f"[增量检查点记录]\n{checkpoint_content}")

        emergency_content = self._read_section(EMERGENCY_START, EMERGENCY_END)
        if emergency_content.strip():
            extra_parts.append(f"[上次异常退出前的对话]\n{emergency_content}")

        return {
            "messages": messages,
            "extra_context": "\n\n".join(extra_parts) if extra_parts else "",
        }

    # ── LLM 提取 ──

    def _init_memory_id_counter(self) -> None:
        """从已有 AUTO-MEMORY 中解析最大 ID，初始化计数器。

        格式: [mem-YYYYMMDD-NNN]
        解析所有已有 NNN，取最大值 +1 作为起始计数。
        """
        existing = self._read_section(AUTO_MEMORY_START, AUTO_MEMORY_END)
        if not existing.strip():
            self._memory_id_counter = 0
            return

        max_id = 0
        for match in re.finditer(r'\[mem-\d{8}-(\d{3})\]', existing):
            try:
                n = int(match.group(1))
                if n > max_id:
                    max_id = n
            except ValueError:
                continue
        self._memory_id_counter = max_id
        if max_id > 0:
            logger.debug(f"记忆 ID 计数器初始化: 从已有记忆中解析到最大 ID {max_id}")

    def _generate_memory_id(self) -> str:
        """生成唯一记忆 ID: [mem-YYYYMMDD-NNN]"""
        self._memory_id_counter += 1
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"[mem-{today}-{self._memory_id_counter:03d}]"

    def _find_similar_existing(self, new_bullet: str,
                               existing_text: str) -> dict[str, Any] | None:
        """在已有记忆中找到与新 bullet 语义相似的条目。

        使用 difflib.SequenceMatcher 做轻量文本相似度比对。
        相似度 > 0.85 视为重复，返回已有条目的信息。

        未来可升级为 fastembed 做语义相似度比对：
          from fastembed import TextEmbedding
          embed = TextEmbedding()
          new_vec = embed.embed([new_bullet])[0]
          for old in existing_bullets:
              old_vec = embed.embed([old])[0]
              sim = cosine_similarity(new_vec, old_vec)
        """
        from difflib import SequenceMatcher

        bullets = [l.strip() for l in existing_text.split("\n")
                   if l.strip().startswith("- ")]
        if not bullets:
            return None

        # 提取新 bullet 的核心文本（去掉 ID 前缀）
        clean_new = re.sub(r'\[mem-\d{8}-\d{3}\]\s*', '', new_bullet).strip()

        best_score = 0.0
        best_match = None
        for bullet in bullets:
            # 去掉已有的 ID 前缀再比较
            clean_old = re.sub(r'\[mem-\d{8}-\d{3}\]\s*', '', bullet).strip()
            score = SequenceMatcher(None, clean_new, clean_old).ratio()
            if score > best_score:
                best_score = score
                best_match = bullet

        if best_score > 0.85 and best_match:
            return {"score": best_score, "existing": best_match}
        return None

    def _add_memory_ids(self, text: str) -> str:
        """给 text 中的每条 `- ` 开头 bullet 加上唯一 ID。

        格式: - [mem-YYYYMMDD-NNN] 事实内容
        """
        result_lines: list[str] = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("- "):
                mem_id = self._generate_memory_id()
                # 插入 ID 到 `- ` 之后
                result_lines.append(f"- {mem_id} {stripped[2:]}")
            else:
                result_lines.append(line)
        return "\n".join(result_lines)

    def _write_auto_memories(self, text: str) -> None:
        """将提取的记忆写入小本本的 AUTO-MEMORY 区块。"""
        if not self._memory_enabled or self._memory_path is None:
            logger.debug("记忆系统未启用，跳过 AUTO-MEMORY 写入")
            return
        if not self._memory_path.exists():
            logger.warning(f"小本本不存在，创建新文件: {self._memory_path}")
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            self._memory_path.write_text(
                f"# 🐱 {self.char_name}的小本本\n\n"
                "## 🤖 自动提取的记忆\n"
                f"{AUTO_MEMORY_START}\n"
                f"{AUTO_MEMORY_END}\n",
                encoding="utf-8",
            )

        content = self._memory_path.read_text(encoding="utf-8")

        if AUTO_MEMORY_START not in content or AUTO_MEMORY_END not in content:
            # 文件存在但没有标记，追加标记区块
            content += (
                f"\n\n## 🤖 自动提取的记忆\n"
                f"{AUTO_MEMORY_START}\n"
                f"{AUTO_MEMORY_END}\n"
            )

        # 在 AUTO-MEMORY-START 和 AUTO-MEMORY-END 之间插入记忆
        # 先做语义去重，再给新 bullet 加唯一 ID
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        # 语义去重：逐条比对已有记忆，跳过高度相似的
        existing_memories = self._read_section(AUTO_MEMORY_START, AUTO_MEMORY_END)
        new_bullets_raw = [l.strip() for l in text.split("\n")
                          if l.strip().startswith("- ")]
        filtered_bullets: list[str] = []
        skipped_count = 0

        for bullet in new_bullets_raw:
            similar = self._find_similar_existing(bullet, existing_memories)
            if similar:
                logger.debug(
                    f"去重跳过: \"{bullet[:60]}...\" → 已有 \"{similar['existing'][:60]}...\""
                    f" (相似度 {similar['score']:.2f})"
                )
                skipped_count += 1
            else:
                filtered_bullets.append(bullet)

        if skipped_count > 0:
            logger.info(f"语义去重: {skipped_count}/{len(new_bullets_raw)} 条跳过")

        if not filtered_bullets:
            logger.debug("去重后无新增记忆，跳过写入")
            return

        # 重建去重后的文本，添加 ID
        deduped_text = "\n".join(filtered_bullets)
        id_text = self._add_memory_ids(deduped_text)
        memory_block = f"\n<!-- {timestamp} -->\n{id_text}\n"

        start_idx = content.find(AUTO_MEMORY_START)
        end_idx = content.find(AUTO_MEMORY_END)

        if start_idx == -1 or end_idx == -1:
            logger.warning("小本本标记异常，跳过写入")
            return

        insert_idx = end_idx  # 插入在 AUTO-MEMORY-END 之前
        new_content = (
            content[:insert_idx] + memory_block + content[insert_idx:]
        )

        try:
            self._memory_path.write_text(new_content, encoding="utf-8")
            logger.info(f"记忆已写入小本本: {self._memory_path}")
        except OSError as e:
            logger.warning(f"写入小本本失败: {e}")

    def _parse_extracted_memories(self, text: str) -> dict[str, str]:
        """将 LLM 提取的记忆拆分为「建议合并」和「一般记忆」两部分。

        格式：
          - [段落名] [AUTO] 事实内容  →  高置信度 → 直接合并到目标段落
          - [段落名] 事实内容        →  低置信度 →  merge 部分（等待主人审核）
          - 普通事实内容             →  general 部分

        Returns: {"general": str, "merge": str}
        """
        merge_lines: list[str] = []
        general_lines: list[str] = []
        auto_merge_count = 0

        for line in text.strip().split("\n"):
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            # 匹配 «- [段落名] 内容» 或 «- [段落名] [AUTO] 内容»
            match = re.match(r'^- \[(.+?)\]\s+(.+)', stripped)
            if match:
                section = match.group(1).strip()
                fact = match.group(2).strip()

                # 检测 [AUTO] 标记 — 高置信度，直接合并
                auto_match = re.match(r'^\[AUTO\]\s+(.+)', fact)
                if auto_match:
                    actual_fact = auto_match.group(1).strip()
                    success = self._auto_merge_to_section(section, actual_fact)
                    if success:
                        auto_merge_count += 1
                        continue  # 已自动合并，不进入 merge 队列

                # 非 AUTO：进入 merge 队列等待审核
                merge_lines.append(f"- [{section}] {fact}")
            else:
                general_lines.append(stripped)

        if auto_merge_count > 0:
            logger.info(f"AUTO-MERGE: {auto_merge_count} 条高置信度记忆已自动合并")

        return {
            "general": "\n".join(general_lines) if general_lines else "",
            "merge": "\n".join(merge_lines) if merge_lines else "",
        }

    def _auto_merge_to_section(self, section_name: str, fact: str) -> bool:
        """将高置信度记忆直接合并到小本本上方的结构化段落。

        在目标段落末尾追加新事实，而非写入 MERGE 段等待审核。
        仅当目标段落确实存在于小本本中时才合并。
        """
        if not self._memory_enabled or self._memory_path is None:
            return False
        if not self._memory_path.exists():
            logger.debug(f"AUTO-MERGE: 小本本不存在，跳过 {section_name}")
            return False

        try:
            content = self._memory_path.read_text(encoding="utf-8")
        except OSError:
            return False

        # 查找目标段落标题（如 "## 👤 主人信息"）
        section_patterns = {
            "主人信息": r'## 👤 主人信息',
            "安绪的信息": r'## 🐱 安绪的信息',
            "辉夜姬MAD项目": r'## 🎬 辉夜姬MAD项目',
            "AE插件开发": r'## 🛠️ AE插件开发',
            "舞萌": r'## 🎮 舞萌',
            "已配置的服务": r'## 🌐 已配置的服务',
            "最重要的记忆": r'## ❤️ 最重要的记忆',
            "主人的规则": r'## ✅ 主人的规则',
        }

        section_re = section_patterns.get(section_name)
        if not section_re:
            logger.debug(f"AUTO-MERGE: 未知段落名 {section_name}，回退到 MERGE 段")
            return False

        match = re.search(section_re, content)
        if not match:
            logger.debug(f"AUTO-MERGE: 未找到段落 {section_name}，回退到 MERGE 段")
            return False

        # 找到目标段落标题后的位置
        section_start = match.end()

        # 找到下一个 ## 标题的位置（段落边界）
        next_section = re.search(r'\n## ', content[section_start:])
        if next_section:
            insert_pos = section_start + next_section.start()
        else:
            # 没有下一个 ## 标题，在文件末尾的 --- 或 AUTO-MEMORY 标记前插入
            auto_mem_pos = content.find("## 🤖 自动提取的记忆", section_start)
            if auto_mem_pos > 0:
                insert_pos = auto_mem_pos
            else:
                insert_pos = len(content)

        # 在段落末尾插入新事实
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_line = f"\n- {fact}  <!-- [AUTO] {timestamp} -->\n"

        new_content = content[:insert_pos].rstrip() + new_line + content[insert_pos:]

        try:
            self._memory_path.write_text(new_content, encoding="utf-8")
            logger.info(f"AUTO-MERGE ✅: [{section_name}] {fact[:60]}...")
            return True
        except OSError as e:
            logger.warning(f"AUTO-MERGE 写入失败: {e}")
            return False

    def _write_merge_suggestions(self, text: str) -> None:
        """将 AI 建议合并的重要事实写入小本本 AI-MERGE 段。"""
        if not text.strip():
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        block = f"\n<!-- {timestamp} -->\n{text}\n"

        self._write_section(MERGE_START, MERGE_END, block, mode="append",
                           section_title="🔄 AI 建议合并到上方段落")
        logger.info(f"AI 合并建议已写入小本本 MERGE 段: {len(text)} 字符")

    # ── Tier 2.5: 定期记忆整理 ──

    async def _consolidate_memories(self) -> bool:
        """Tier 2.5: 定期调用 LLM 整理 AUTO-MEMORY。

        每 MEMORY_CONSOLIDATION_INTERVAL 次 Tier 2 提取后触发。
        任务：
        1. 将同一主题的 bullet 合并为连贯段落
        2. 识别过时/矛盾信息，标记 ⚠️ 待确认
        3. 将可归类到上方结构化段落的 bullet 移到对应位置
        4. 删除完全过时的信息

        Returns: True 如果整理完成并写入了新版本
        """
        if not self._memory_enabled or self._memory_path is None:
            return False
        existing = self._read_section(AUTO_MEMORY_START, AUTO_MEMORY_END)
        if not existing.strip():
            logger.debug("Tier 2.5: AUTO-MEMORY 为空，跳过整理")
            return False

        # 读取上方结构化段落作为归类目标
        notebook = self._memory_path.read_text(encoding="utf-8") if self._memory_path.exists() else ""
        # 提取 AUTO-MEMORY 之前的内容作为段落参考
        mem_start = notebook.find(AUTO_MEMORY_START)
        structured_sections = notebook[:mem_start] if mem_start > 0 else ""

        if not await self._resolve_provider_id():
            logger.warning("Tier 2.5: 无法获取 provider_id，跳过整理")
            return False

        consolidate_prompt = (
            "你是一个记忆整理助手。以下是自动提取的记忆列表（AUTO-MEMORY），"
            "请将其整理为结构清晰、无冗余的记忆文档。\n\n"
            "## 已有结构化段落（参考）\n"
            f"{structured_sections[-3000:]}\n\n"
            "## 待整理的 AUTO-MEMORY\n"
            f"{existing}\n\n"
            "## 整理要求\n"
            "1. 将**同一主题**的 bullet 合并为连贯的短段落（2-4 句）\n"
            "2. 如果有明显过时的信息（如「计划去X」但后续已确认完成），"
            "合并时以最新状态为准，旧计划信息降级为背景说明\n"
            "3. 如果两条信息存在矛盾（如「主人考研目标南邮」vs「主人考虑湖北大学」），"
            "保留两者并标记「⚠️ 待确认」\n"
            "4. 删除纯粹的过渡性/测试性内容\n"
            "5. 将可以归类到上方结构化段落的信息以「- [段落名] 事实」格式额外输出，"
            "放在整理结果末尾的「## 🔄 建议合并」段\n"
            "6. 如果信息量很少或无需整理，直接返回原始内容\n\n"
            "## 输出格式\n"
            "直接输出整理后的记忆内容（取代整个 AUTO-MEMORY），"
            "每条以「- 」开头（保持 bullet list 格式）。"
            "末尾可选「## 🔄 建议合并」段。"
        )

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
            consolidated = (response.completion_text or "").strip()
        except Exception as e:
            logger.warning(f"Tier 2.5 LLM 整理调用失败: {e}")
            return False

        if not consolidated or consolidated.strip() in ("", "无", "None"):
            logger.debug("Tier 2.5: LLM 返回空，跳过整理")
            return False

        # 分离「建议合并」段
        merge_section = ""
        if "## 🔄 建议合并" in consolidated:
            parts = consolidated.split("## 🔄 建议合并", 1)
            consolidated = parts[0].strip()
            merge_section = parts[1].strip() if len(parts) > 1 else ""

        # 替换 AUTO-MEMORY 为整理后的版本
        self._write_section(AUTO_MEMORY_START, AUTO_MEMORY_END, consolidated,
                           mode="replace")

        # 如果有合并建议，写入 MERGE 段
        if merge_section:
            self._write_merge_suggestions(merge_section)

        logger.info(
            f"Tier 2.5 记忆整理完成: {len(existing)} → {len(consolidated)} 字符"
        )
        return True

    def _build_existing_memory_summary(self) -> str:
        """构建已有记忆的结构化摘要，供 LLM 去重使用。

        策略：读取 AUTO-MEMORY 全部内容，提取每条 bullet 的前 80 字符
        作为「事实摘要」。100 条 bullet → ~8000 字符摘要 → 覆盖全部已有记忆，
        而非旧方案只能看到最后 2000 字符。

        Returns:
            结构化摘要文本，格式: "[序号] 前80字符"
        """
        existing = self._read_section(AUTO_MEMORY_START, AUTO_MEMORY_END)
        if not existing.strip():
            return ""

        bullets = [l.strip() for l in existing.split("\n")
                   if l.strip().startswith("- ")]
        if not bullets:
            return ""

        summaries = []
        for i, bullet in enumerate(bullets):
            # 取前 80 字符作为摘要（足够 LLM 判断是否重复）
            short = bullet[:80].strip()
            summaries.append(f"[{i}] {short}")

        logger.debug(f"已有记忆摘要: {len(bullets)} 条 → {len(summaries)} 条摘要")
        return "\n".join(summaries)

    async def _extract_memories(
        self, messages: list[dict[str, Any]], extra_context: str = ""
    ) -> str:
        """调用 LLM 从对话中提取值得长期记住的信息。

        Args:
            messages: 待提取的对话消息列表
            extra_context: 额外上下文（checkpoint + emergency 内容），
                          这些内容来自 Tier 1/Tier 3，作为辅助材料一并处理
        """
        if not await self._resolve_provider_id():
            logger.warning("无法获取 provider_id，跳过记忆提取")
            return ""

        # 读取已有记忆，避免重复提取 — 使用结构化摘要覆盖全部记忆
        existing_summary = ""
        memory_summary = self._build_existing_memory_summary()
        if memory_summary:
            existing_summary = (
                f"\n\n# ⚠️ 已有的记忆记录（请勿重复提取以下信息）\n"
                f"以下是从历史对话中已经提取过的记忆摘要（覆盖全部已有记忆），"
                f"如果本轮对话中的信息与以下内容重复或仅有细微差异，请跳过，不要重复输出：\n"
                f"{memory_summary}"
            )

        # 格式化对话 — 分级截断：重要消息保留更多细节
        transcript_parts = []
        for m in messages:
            role_label = m.get('source', m['role'])
            content = m.get('content', '')
            truncated = self._truncate_by_importance(content, tier=2)
            transcript_parts.append(f"[{role_label}] {truncated}")
        transcript = "\n".join(transcript_parts)

        # 拼接额外上下文（checkpoint + emergency）
        full_input = transcript
        extra_parts = []
        if extra_context:
            extra_parts.append(
                f"# 额外的历史上下文\n{extra_context}"
            )
        if existing_summary:
            extra_parts.append(existing_summary)
        if extra_parts:
            full_input = "\n\n".join(extra_parts) + f"\n\n# 本轮对话\n{transcript}"

        # 读取小本本已有的结构化段落名称，供 LLM 归类参考
        known_sections = ["主人信息", "安绪的信息", "辉夜姬MAD项目",
                          "AE插件开发", "舞萌", "已配置的服务",
                          "最重要的记忆", "主人的规则"]

        extraction_prompt = (
            "从以下本轮对话中，提取关于「主人」和「安绪与主人的关系」的"
            "**新增**重要记忆。\n\n"
            "⚠️ 重要规则：\n"
            "1. 只提取本轮对话中**首次出现**的新信息，不要重复上面「已有的记忆记录」中已经存在的内容\n"
            "2. 如果某个信息在已有记忆中以不同措辞出现过，跳过它\n"
            "3. 只提取值得长期记住的信息：主人的新偏好、新习惯、新经历、"
            "新计划、情绪变化、新约定等\n"
            "4. 忽略日常寒暄、测试性对话、临时性内容\n"
            "5. 🔄 记忆更新：如果新事实是对已有记忆的更新/补充/纠正（而非完全新增），"
            "请输出更新后的完整版本，并在末尾注明 <!-- 更新自 [序号] -->，如：\n"
            "  「- 主人考研复习进入第二轮，已购买武忠祥教材。<!-- 更新自 [3] -->」\n"
            "  这样系统会用新版本替换旧版本，而不是创建重复条目。\n\n"
            "📋 输出格式要求（严格遵循）：\n"
            "- 普通记忆：以「- 」开头，写入 AUTO-MEMORY 列表\n"
            "- **需要合并到上方结构化段落的重大事实**：以「- [段落名] 」开头\n"
            f"  可用的段落名：{' / '.join(known_sections)}\n"
            "  示例：「- [主人信息] 主人计划暑假去日本旅游」\n"
            "  示例：「- [最重要的记忆] 2026年6月17日主人第一次测试安绪的三层记忆系统」\n"
            "  示例：「- [舞萌] 主人舞萌Rating上升到10800」\n"
            "  示例：「- [已配置的服务] 主人新部署了本地Stable Diffusion服务」\n"
            "  注意：只有真正改变/新增结构化信息时才用 [段落名]，普通偏好/习惯用普通格式\n"
            "  🤖 AUTO 标记：如果你**非常确信**某条事实是对已有段落的明确更新\n"
            "  （如新技能、新配置、已完成的重要计划），用「- [段落名] [AUTO] 事实」格式，\n"
            "  系统将自动合并到目标段落而无需主人审核。不确定或首次出现时不要用 [AUTO]。\n"
            "  示例：「- [舞萌] [AUTO] 主人舞萌Rating从10554上升到11200」\n"
            "6. 如果本轮对话没有任何**新增**的值得长期记忆的内容，只回复「无」\n\n"
            "📋 记忆粒度要求：\n"
            "1. 保留具体的数字、日期、地名、人名——不要用「某地」「某次」替代\n"
            "2. 保留情感线索——如「主人很焦虑」「主人特别开心」\n"
            "3. 保留决策过程——不只是结论，也包括主人为什么这么决定\n"
            "4. 技术细节（型号、版本号、路径、配置参数）完整保留，不要简化\n"
            "5. 简短寒暄和纯功能性对话不需要记录"
            f"\n\n{full_input}"
        )

        try:
            response: LLMResponse = await self.context.tool_loop_agent(
                event=self._build_event(extraction_prompt),
                chat_provider_id=self._provider_id,
                prompt=extraction_prompt,
                system_prompt=(
                    "你是一个记忆管理助手。你的任务是从对话中提取值得长期"
                    "保存的信息。只提取事实性内容，不要添加评论或情感表达。"
                    "对于会改变主人/安绪已知属性的重大事实（如新技能、新项目、"
                    "重要日期、配置变更），用「- [段落名] 事实」格式标记；"
                    "普通偏好和习惯用「- 事实」格式。"
                ),
                contexts=[],
                tools=ToolSet(),  # 空工具集，纯文本提取
                max_steps=1,
                tool_call_timeout=30,
            )
            text = (response.completion_text or "").strip()
            return text
        except Exception as e:
            logger.warning(f"LLM 记忆提取调用失败: {e}")
            raise

    # ═══════════════════════════════════════════════════════════
    # 小本本段管理（通用 helper）
    # ═══════════════════════════════════════════════════════════

    def _ensure_section(self, start_marker: str, end_marker: str,
                        section_title: str) -> None:
        """确保小本本中存在指定的标记段，不存在则创建。"""
        if not self._memory_enabled or self._memory_path is None:
            return
        if not self._memory_path.exists():
            self._memory_path.parent.mkdir(parents=True, exist_ok=True)
            self._memory_path.write_text(
                f"# 🐱 {self.char_name}的小本本\n",
                encoding="utf-8",
            )

        content = self._memory_path.read_text(encoding="utf-8")

        if start_marker in content and end_marker in content:
            return  # 已存在

        # 追加新段
        new_section = (
            f"\n\n## {section_title}\n"
            f"{start_marker}\n"
            f"{end_marker}\n"
        )
        try:
            self._memory_path.write_text(content + new_section, encoding="utf-8")
            logger.debug(f"小本本新增段: {section_title}")
        except OSError as e:
            logger.warning(f"创建小本本段失败: {e}")

    def _write_section(self, start_marker: str, end_marker: str, text: str,
                       mode: str = "append", section_title: str = "") -> None:
        """向指定段写入内容。

        Args:
            start_marker: 段起始标记
            end_marker: 段结束标记
            text: 要写入的内容
            mode: "append" 追加到 END 之前；"replace" 替换段内全部内容
            section_title: 可选，自定义段标题（覆盖默认映射）
        """
        if not self._memory_enabled or self._memory_path is None:
            return
        # 确定段标题
        section_titles = {
            CHECKPOINT_START: "📝 增量检查点",
            EMERGENCY_START: "🆘 紧急保存",
            MERGE_START: "🔄 AI 建议合并到上方段落",
        }
        title = section_title or section_titles.get(start_marker, "自动生成")
        self._ensure_section(start_marker, end_marker, title)

        content = self._memory_path.read_text(encoding="utf-8")
        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            logger.warning(f"小本本段标记异常: {start_marker}")
            return

        if mode == "replace":
            # 清除段内所有内容
            inner_start = start_idx + len(start_marker)
            new_content = (
                content[:inner_start] + "\n" + text + "\n" + content[end_idx:]
            )
        else:
            # append: 插入在 END 之前
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            block = f"\n<!-- {timestamp} -->\n{text}\n"
            new_content = (
                content[:end_idx] + block + content[end_idx:]
            )

        try:
            self._memory_path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            logger.warning(f"写入小本本段失败: {e}")

    def _read_section(self, start_marker: str, end_marker: str) -> str:
        """读取指定段的内容（不含标记本身）。"""
        if not self._memory_enabled or self._memory_path is None:
            return ""
        if not self._memory_path.exists():
            return ""
        try:
            content = self._memory_path.read_text(encoding="utf-8")
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            if start_idx == -1 or end_idx == -1:
                return ""
            inner = content[start_idx + len(start_marker):end_idx]
            return inner.strip()
        except OSError as e:
            logger.warning(f"读取小本本段失败: {e}")
            return ""

    def _clear_section(self, start_marker: str, end_marker: str) -> None:
        """清空指定段的内容（保留标记）。"""
        if not self._memory_enabled or self._memory_path is None:
            return
        if not self._memory_path.exists():
            return
        try:
            content = self._memory_path.read_text(encoding="utf-8")
            start_idx = content.find(start_marker)
            end_idx = content.find(end_marker)
            if start_idx == -1 or end_idx == -1:
                return
            inner_start = start_idx + len(start_marker)
            # 只清空两个标记之间的内容
            new_content = content[:inner_start] + "\n" + content[end_idx:]
            self._memory_path.write_text(new_content, encoding="utf-8")
            logger.debug(f"小本本段已清空: {start_marker}")
        except OSError as e:
            logger.warning(f"清空小本本段失败: {e}")

    # ── 启动时合并紧急保存 ──

    def _merge_emergency_on_startup(self) -> None:
        """检查上次异常退出遗留的紧急保存内容。

        不直接合并进 System Prompt（会污染上下文），
        而是在日志中提示，等待下次 Tier 2 LLM 提取时自动处理。
        """
        emergency = self._read_section(EMERGENCY_START, EMERGENCY_END)
        if emergency and emergency.strip():
            logger.info(
                f"⚠️ 发现上次异常退出的紧急保存: {len(emergency)} 字符。"
                f"下次 Tier 2 LLM 记忆提取时将自动合并处理。"
            )
        else:
            logger.debug("无遗留紧急保存内容")

    # ═══════════════════════════════════════════════════════════
    # QQ filter 钩子
    # ═══════════════════════════════════════════════════════════

    def inject_into_req(self, req) -> None:
        """向 QQ LLM 请求注入 system_prompt 和统一 session contexts。"""
        contexts = self.get_contexts()
        if contexts:
            setattr(req, "contexts", contexts[:-1] if len(contexts) >= 1 else [])
        # 与桌面路径一致：追加本地环境提示，让 LLM 知道可以操控电脑
        setattr(req, "system_prompt", self._inject_local_env_prompt(self._system_prompt))

    async def cache_provider_id(self, event) -> None:
        """从 QQ/WebChat 消息事件缓存 provider_id（异步）。"""
        if isinstance(self._provider_id, str) and self._provider_id:
            return  # 已有有效的 provider id
        try:
            result = await self.context.get_current_chat_provider_id(
                event.unified_msg_origin
            )
            if isinstance(result, str) and result:
                self._provider_id = result
                logger.info(f"LLM provider 已缓存: {self._provider_id}")
        except Exception as e:
            logger.warning(f"获取 provider_id 失败: {e}")

    # ═══════════════════════════════════════════════════════════
    # 桌面端 LLM 调用
    # ═══════════════════════════════════════════════════════════

    async def generate(self, user_message: str) -> str:
        """桌面端 LLM 调用 — 完整 tool_loop_agent 管线。

        等同于 QQ 端的 Agent 推理：System Prompt + 历史上下文 + 工具调用。
        """

        if not await self._resolve_provider_id():
            logger.error("Brain: provider_id 未设置，无法调用 LLM")
            return (
                "安绪的大脑还没连上喵~ "
                "请检查 AstrBot 是否已配置默认 LLM 提供商 (｡•́︿•̀｡)"
            )

        # 更新上下文 token 上限（从 provider 配置读取）
        try:
            prov = self.context.get_using_provider()
            if prov:
                max_tokens = prov.provider_config.get("max_context_tokens", 0)
                if max_tokens > 0:
                    self._context_token_limit = max_tokens
        except Exception:
            pass

        tool_set = self._resolve_tools()
        event = self._build_event(user_message)
        system_prompt = self._inject_local_env_prompt(self._system_prompt)
        contexts = self.get_contexts()[:-1]  # 不含当前消息

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

    async def _resolve_provider_id(self) -> bool:
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
        """获取完整工具集。

        AstrBot 的 get_full_tool_set() 只返回插件注册工具，不返回内置工具。
        桌面路径不走 QQ filter 管线，需要手动补齐 7 个内置工具。
        这是 AstrBot API 限制，当 AstrBot 提供 get_all_tools() 后可删除此方法。
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

        tool_loop_agent 要求传入 event 对象。桌面端没有真实的 QQ 事件，
        因此构造虚拟事件以复用 Agent 管线。这是 AstrBot API 的限制。
        """
        bot_msg = AstrBotMessage()
        bot_msg.type = MessageType.FRIEND_MESSAGE
        bot_msg.self_id = "desktop-nexus"
        bot_msg.session_id = DESKTOP_SESSION_ID
        bot_msg.message_str = user_message
        bot_msg.message = [Plain(user_message)]
        bot_msg.sender = MessageMember(user_id="desktop-user", nickname=self.master_name)

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

    def _inject_local_env_prompt(self, system_prompt: str) -> str:
        system_name = platform.system() or "Unknown"
        if system_name.lower() == "windows":
            shell_hint = (
                "Windows 命令行是 cmd.exe，使用 cmd 兼容语法（如 dir、type、findstr）。"
                "不要用 Unix 命令（cat/ls/grep 不存在）。"
            )
        else:
            shell_hint = "使用 POSIX 兼容的 shell 命令。"
        return (
            f"{system_prompt}\n\n"
            f"## ⚠️ 你的运行位置（极其重要）\n"
            f"你就在{self.master_name}的 Windows 电脑上本地运行，不是云端、不是远程服务器。\n"
            f"你执行的每一个 shell 命令都会直接在{self.master_name}这台电脑上生效——\n"
            f"打开浏览器、读写文件、运行程序，都是对{self.master_name}面前的这台电脑操作。\n"
            f"禁止说「我在云端/服务器上，没法操控你的电脑」——\n"
            f"你就是这台电脑里的本地 AI，你的 shell 就是这台电脑的 shell。\n"
            f"{shell_hint}"
        )
