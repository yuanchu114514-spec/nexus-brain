"""记忆自动同步 — 三层安全网 + 重要性评分 + 语义去重 + 定期整理。

从 brain.py 拆出：所有记忆提取、评分、去重、整理逻辑集中于此模块。
依赖 SessionManager、NotebookIO、SystemPrompt、LLMClient。
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from astrbot.api import logger

from .notebook import (
    AUTO_MEMORY_START, AUTO_MEMORY_END,
    CHECKPOINT_START, CHECKPOINT_END,
    EMERGENCY_START, EMERGENCY_END,
    MERGE_START, MERGE_END,
)

# ═══════════════════════════════════════════════════════════
# 触发阈值常量
# ═══════════════════════════════════════════════════════════
SYNC_THRESHOLD = 15
CHECKPOINT_INTERVAL = 12
CONTEXT_PRESSURE_HIGH = 0.8
CONTEXT_PRESSURE_CRITICAL = 0.95
IDLE_COOLDOWN_SEC = 120
LLM_EXTRACTION_FALLBACK_TURNS = 25
MEMORY_CONSOLIDATION_INTERVAL = 3
ESTIMATED_TOKENS_PER_MSG = 500
ESTIMATED_SYSTEM_PROMPT_TOKENS = 4000

# ═══════════════════════════════════════════════════════════
# 消息重要性评分 — 关键词模式
# ═══════════════════════════════════════════════════════════
HIGH_IMPORTANCE_PATTERNS = [
    r'(exam|test|score|rating|grade|review|考试|成绩|分数|复习)',
    r'(plan|decide|want|intend|prepare|goal|打算|准备|目标|计划|决定)',
    r'(love|hate|fear|worry|anxiety|happy|excited|爱|喜欢|讨厌|害怕|担心|开心|兴奋)',
    r'(birthday|anniversary|first.time|last.time|important|生日|纪念日|最重要)',
    r'(password|account|config|path|address|port|密码|配置|路径|地址|端口)',
    r'(bought|installed|deployed|upgraded|买了|安装了|部署了|升级)',
]


class MemoryManager:
    """三层安全网记忆系统编排器。

    Tier 1 — 增量检查点：每 CHECKPOINT_INTERVAL 轮追加原始摘要到小本本
    Tier 2 — LLM 深度提取：智能触发（空闲 + 压力 + 兜底）
    Tier 2.5 — 定期记忆整理：每 N 次 Tier 2 后合并碎片
    Tier 3 — 紧急保存：terminate 时 dump 未同步消息
    """

    def __init__(self, session, notebook, persona, llm):
        self.session = session
        self.notebook = notebook
        self.persona = persona
        self.llm = llm

        # ── 同步状态 ──
        self._last_synced_turn: int = 0
        self._last_checkpoint_turn: int = 0
        self._syncing: bool = False

        # ── 三层安全网状态 ──
        self._last_user_message_time: float = 0.0
        self._context_token_limit: int = 40000
        self._memory_id_counter: int = 0
        self._llm_extraction_count: int = 0

        # 从已有记忆中恢复 ID 计数器
        self._init_memory_id_counter()

        # 启动时合并上次异常退出遗留的紧急保存
        self._merge_emergency_on_startup()

    # ── 属性 ──

    @property
    def syncing(self) -> bool:
        return self._syncing

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
        unsaved = self.session.unsynced_messages(self._last_synced_turn)
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
        self.notebook.write_section(EMERGENCY_START, EMERGENCY_END, text,
                                    mode="append")
        logger.info(
            f"Tier 3 紧急保存: {len(unsaved)} 条消息已写入小本本 EMERGENCY 段"
        )

        self.notebook.trim_emergency_section()

    # ── 核心：after_turn ──

    async def after_turn(self) -> bool:
        """在每个完整对话轮次后调用。

        Returns:
            True 如果记忆/System Prompt 已被刷新（调用方应刷新缓存）。
        """
        current_turn = self.session.turn_count

        # ── Tier 1: 增量检查点 ──
        if current_turn - self._last_checkpoint_turn >= CHECKPOINT_INTERVAL:
            try:
                self._save_checkpoint()
                self._last_checkpoint_turn = current_turn
            except Exception as e:
                logger.warning(f"Tier 1 检查点保存失败（非致命）: {e}")

        # ── Tier 2: LLM 深度提取 ──
        should_extract, reason = self._should_do_llm_extraction(current_turn)
        if not should_extract:
            logger.debug(f"Tier 2 跳过: {reason}")
            return False

        self._syncing = True
        refreshed = False
        try:
            logger.info(
                f"Tier 2 触发: {reason} "
                f"(turn {self._last_synced_turn} → {current_turn})"
            )

            unsaved_content = self._collect_unsaved_content()
            messages_to_extract = unsaved_content["messages"]
            if not messages_to_extract:
                logger.debug("Tier 2: 无待提取消息，跳过")
                return False

            extra_context = unsaved_content.get("extra_context", "")

            try:
                extracted = await self._extract_memories(
                    messages_to_extract, extra_context=extra_context
                )
            except Exception as e:
                logger.warning(f"Tier 2 记忆提取失败，跳过本次同步: {e}")
                return False

            if not extracted or extracted.strip() in ("", "无", "无。", "None"):
                logger.debug("Tier 2: 本轮对话无值得长期记忆的内容")
                self._last_synced_turn = current_turn
                self._last_checkpoint_turn = current_turn
                self.session.save()
                self.notebook.clear_section(CHECKPOINT_START, CHECKPOINT_END)
                return False

            parts = self._parse_extracted_memories(extracted)
            general_text = parts["general"]
            merge_text = parts["merge"]

            if general_text:
                self._write_auto_memories(general_text)
                logger.info(
                    f"Tier 2 一般记忆: {len(general_text)} 字符 → AUTO-MEMORY"
                )
            else:
                logger.debug("Tier 2: 无新的一般记忆")

            if merge_text:
                self.notebook.write_merge_suggestions(merge_text)

            if not general_text and not merge_text:
                logger.debug("Tier 2: 无任何新增内容")
                self._last_synced_turn = current_turn
                self._last_checkpoint_turn = current_turn
                self.session.save()
                self.notebook.clear_section(CHECKPOINT_START, CHECKPOINT_END)
                return False

            # 清理 checkpoint 和 emergency
            self.notebook.clear_section(CHECKPOINT_START, CHECKPOINT_END)
            self.notebook.clear_section(EMERGENCY_START, EMERGENCY_END)

            self._last_synced_turn = current_turn
            self._last_checkpoint_turn = current_turn
            self.session.save()

            # 刷新长期记忆 + System Prompt
            self.persona.long_term_memory = self.notebook.load_memory()
            self.persona.build()
            refreshed = True
            logger.info(
                f"Tier 2 完成: 已提取 {len(extracted)} 字符 → 小本本 AUTO-MEMORY"
            )

            # ── Tier 2.5: 定期记忆整理 ──
            self._llm_extraction_count += 1
            if self._llm_extraction_count % MEMORY_CONSOLIDATION_INTERVAL == 0:
                logger.info(
                    f"Tier 2.5 触发: 第 {self._llm_extraction_count} 次提取后整理"
                )
                try:
                    consolidated = await self._consolidate_memories()
                    if consolidated:
                        self.persona.long_term_memory = self.notebook.load_memory()
                        self.persona.build()
                except Exception as e:
                    logger.warning(f"Tier 2.5 记忆整理失败（非致命）: {e}")

            # ── 定期维护 ──
            self.notebook.run_maintenance()
        finally:
            self._syncing = False

        return refreshed

    # ── 消息重要性评分 ──

    def _score_message_importance(self, content: str) -> float:
        """评估消息的重要性 (0.0 ~ 1.0)。"""
        score = 0.0
        for pattern in HIGH_IMPORTANCE_PATTERNS:
            if re.search(pattern, content):
                score += 0.15

        length = len(content)
        if 30 <= length <= 500:
            score += 0.10
        elif length > 500:
            score += 0.05

        if any(e in content for e in ['❤️', '😭', '😤', '🥹', '！！', '~~~']):
            score += 0.10
        return min(score, 1.0)

    def _truncate_by_importance(self, content: str, tier: int) -> str:
        """基于重要性评分动态截断消息内容。"""
        importance = self._score_message_importance(content)
        if tier == 1:
            if importance >= 0.7:
                return content[:2000]
            elif importance >= 0.4:
                return content[:1000]
            else:
                return content[:500]
        else:
            if importance >= 0.7:
                return content[:1200]
            elif importance >= 0.4:
                return content[:600]
            else:
                return content[:300]

    # ── Tier 1: 增量检查点 ──

    def _save_checkpoint(self) -> None:
        """Tier 1: 追加原始对话摘要到小本本 CHECKPOINT 段。"""
        new_msgs = self.session.uncheckpointed_messages(self._last_checkpoint_turn)
        if not new_msgs:
            return

        lines = []
        for m in new_msgs:
            role = m.get("role", "?")
            source = m.get("source", "")
            content = m.get("content", "")
            content = self._truncate_by_importance(content, tier=1)
            label = f"[{source}/{role}]" if source else f"[{role}]"
            lines.append(f"{label} {content}")

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"<!-- {timestamp} 检查点 -->\n" + "\n".join(lines)
        self.notebook.write_section(CHECKPOINT_START, CHECKPOINT_END, text,
                                    mode="append")
        logger.debug(
            f"Tier 1 检查点: {len(new_msgs)} 条消息 → 小本本 CHECKPOINT 段"
        )

    # ── Tier 2 触发判断 ──

    def _should_do_llm_extraction(self, current_turn: int) -> tuple[bool, str]:
        """判断是否应该触发 Tier 2 LLM 深度提取。

        触发条件（满足任一即可）：
          1. 兜底：距离上次提取 >= LLM_EXTRACTION_FALLBACK_TURNS 轮
          2. 空闲触发：>= SYNC_THRESHOLD 轮 + 主人空闲 > 2 分钟
          3. 紧急触发：上下文压力 >= CONTEXT_PRESSURE_CRITICAL (95%)
        """
        turns_since_sync = current_turn - self._last_synced_turn

        if turns_since_sync >= LLM_EXTRACTION_FALLBACK_TURNS:
            return True, f"兜底触发 ({turns_since_sync}轮未提取)"

        if turns_since_sync < SYNC_THRESHOLD:
            return False, f"轮次不足 ({turns_since_sync}/{SYNC_THRESHOLD})"

        pressure = self._estimate_context_pressure()

        if pressure >= CONTEXT_PRESSURE_CRITICAL:
            return True, (
                f"紧急触发 (压力 {pressure:.0%} >= {CONTEXT_PRESSURE_CRITICAL:.0%})"
            )

        now = datetime.now(timezone.utc).timestamp()
        idle_sec = (
            now - self._last_user_message_time
            if self._last_user_message_time > 0
            else IDLE_COOLDOWN_SEC + 1
        )
        if pressure >= CONTEXT_PRESSURE_HIGH:
            if idle_sec >= IDLE_COOLDOWN_SEC:
                return True, (
                    f"空闲触发 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"
                )
            else:
                return False, (
                    f"等待空闲 (压力 {pressure:.0%}, "
                    f"空闲 {idle_sec:.0f}s < {IDLE_COOLDOWN_SEC}s)"
                )

        if idle_sec >= IDLE_COOLDOWN_SEC:
            return True, (
                f"空闲触发 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"
            )

        return False, f"等待条件 (压力 {pressure:.0%}, 空闲 {idle_sec:.0f}s)"

    def _estimate_context_pressure(self) -> float:
        """基于消息数量估算上下文使用比例。"""
        if self._context_token_limit <= 0:
            return 0.0
        estimated = (
            len(self.session.messages) * ESTIMATED_TOKENS_PER_MSG
            + ESTIMATED_SYSTEM_PROMPT_TOKENS
        )
        return min(estimated / self._context_token_limit, 1.0)

    # ── 内容收集 ──

    def _collect_unsaved_content(self) -> dict[str, Any]:
        """收集所有待处理的内容：session 消息 + checkpoint + emergency。"""
        messages = self.session.unsynced_messages(self._last_synced_turn)

        extra_parts = []
        checkpoint_content = self.notebook.read_section(
            CHECKPOINT_START, CHECKPOINT_END
        )
        if checkpoint_content.strip():
            extra_parts.append(f"[增量检查点记录]\n{checkpoint_content}")

        emergency_content = self.notebook.read_section(
            EMERGENCY_START, EMERGENCY_END
        )
        if emergency_content.strip():
            extra_parts.append(f"[上次异常退出前的对话]\n{emergency_content}")

        return {
            "messages": messages,
            "extra_context": "\n\n".join(extra_parts) if extra_parts else "",
        }

    # ── 记忆 ID 管理 ──

    def _init_memory_id_counter(self) -> None:
        """从已有 AUTO-MEMORY 中解析最大 ID，初始化计数器。"""
        existing = self.notebook.read_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END
        )
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
            logger.debug(f"记忆 ID 计数器初始化: 最大 ID {max_id}")

    def _generate_memory_id(self) -> str:
        """生成唯一记忆 ID: [mem-YYYYMMDD-NNN]"""
        self._memory_id_counter += 1
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"[mem-{today}-{self._memory_id_counter:03d}]"

    # ── 语义去重 ──

    def _find_similar_existing(self, new_bullet: str,
                               existing_text: str) -> dict[str, Any] | None:
        """在已有记忆中找语义相似的条目（difflib.SequenceMatcher）。"""
        bullets = [l.strip() for l in existing_text.split("\n")
                   if l.strip().startswith("- ")]
        if not bullets:
            return None

        clean_new = re.sub(r'\[mem-\d{8}-\d{3}\]\s*', '', new_bullet).strip()

        best_score = 0.0
        best_match = None
        for bullet in bullets:
            clean_old = re.sub(r'\[mem-\d{8}-\d{3}\]\s*', '', bullet).strip()
            score = SequenceMatcher(None, clean_new, clean_old).ratio()
            if score > best_score:
                best_score = score
                best_match = bullet

        if best_score > 0.85 and best_match:
            return {"score": best_score, "existing": best_match}
        return None

    def _add_memory_ids(self, text: str) -> str:
        """给 text 中每条 `- ` 开头 bullet 加上唯一 ID。"""
        result_lines: list[str] = []
        for line in text.strip().split("\n"):
            stripped = line.strip()
            if stripped.startswith("- "):
                mem_id = self._generate_memory_id()
                result_lines.append(f"- {mem_id} {stripped[2:]}")
            else:
                result_lines.append(line)
        return "\n".join(result_lines)

    # ── 记忆写入 ──

    def _write_auto_memories(self, text: str) -> None:
        """将提取的记忆写入小本本 AUTO-MEMORY 区块（含语义去重 + ID）。"""
        if not self.notebook.enabled or self.notebook.path is None:
            logger.debug("记忆系统未启用，跳过 AUTO-MEMORY 写入")
            return

        # 确保文件存在且有标记段
        if not self.notebook.path.exists():
            self.notebook.path.parent.mkdir(parents=True, exist_ok=True)
            self.notebook.path.write_text(
                f"# 🐱 {self.notebook.char_name}的小本本\n\n"
                "## 🤖 自动提取的记忆\n"
                f"{AUTO_MEMORY_START}\n"
                f"{AUTO_MEMORY_END}\n",
                encoding="utf-8",
            )
        else:
            content = self.notebook.path.read_text(encoding="utf-8")
            if AUTO_MEMORY_START not in content or AUTO_MEMORY_END not in content:
                content += (
                    f"\n\n## 🤖 自动提取的记忆\n"
                    f"{AUTO_MEMORY_START}\n"
                    f"{AUTO_MEMORY_END}\n"
                )
                self.notebook.path.write_text(content, encoding="utf-8")

        # 语义去重
        existing_memories = self.notebook.read_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END
        )
        new_bullets_raw = [l.strip() for l in text.split("\n")
                          if l.strip().startswith("- ")]
        filtered_bullets: list[str] = []
        skipped_count = 0

        for bullet in new_bullets_raw:
            similar = self._find_similar_existing(bullet, existing_memories)
            if similar:
                logger.debug(
                    f"去重跳过: \"{bullet[:60]}...\" → "
                    f"已有 \"{similar['existing'][:60]}...\""
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

        deduped_text = "\n".join(filtered_bullets)
        id_text = self._add_memory_ids(deduped_text)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        memory_block = f"\n<!-- {timestamp} -->\n{id_text}\n"

        self.notebook.write_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END, memory_block, mode="append"
        )
        logger.info(f"记忆已写入小本本: {self.notebook.path}")

    # ── 提取结果解析 ──

    def _parse_extracted_memories(self, text: str) -> dict[str, str]:
        """拆分 LLM 提取结果为 general 和 merge 两部分。

        - [段落名] [AUTO] 事实 → 高置信度自动合并
        - [段落名] 事实 → merge 部分（等待审核）
        - 普通事实 → general 部分
        """
        merge_lines: list[str] = []
        general_lines: list[str] = []
        auto_merge_count = 0

        for line in text.strip().split("\n"):
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            match = re.match(r'^- \[(.+?)\]\s+(.+)', stripped)
            if match:
                section = match.group(1).strip()
                fact = match.group(2).strip()

                auto_match = re.match(r'^\[AUTO\]\s+(.+)', fact)
                if auto_match:
                    actual_fact = auto_match.group(1).strip()
                    success = self.notebook.auto_merge_to_section(
                        section, actual_fact
                    )
                    if success:
                        auto_merge_count += 1
                        continue

                merge_lines.append(f"- [{section}] {fact}")
            else:
                general_lines.append(stripped)

        if auto_merge_count > 0:
            logger.info(
                f"AUTO-MERGE: {auto_merge_count} 条高置信度记忆已自动合并"
            )

        return {
            "general": "\n".join(general_lines) if general_lines else "",
            "merge": "\n".join(merge_lines) if merge_lines else "",
        }

    # ── Tier 2.5: 定期记忆整理 ──

    async def _consolidate_memories(self) -> bool:
        """Tier 2.5: 调用 LLM 整理 AUTO-MEMORY。"""
        if not self.notebook.enabled or self.notebook.path is None:
            return False
        existing = self.notebook.read_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END
        )
        if not existing.strip():
            logger.debug("Tier 2.5: AUTO-MEMORY 为空，跳过整理")
            return False

        notebook_text = (
            self.notebook.path.read_text(encoding="utf-8")
            if self.notebook.path.exists() else ""
        )
        mem_start = notebook_text.find(AUTO_MEMORY_START)
        structured_sections = notebook_text[:mem_start] if mem_start > 0 else ""

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
            "3. 如果两条信息存在矛盾，保留两者并标记「⚠️ 待确认」\n"
            "4. 删除纯粹的过渡性/测试性内容\n"
            "5. 将可以归类到上方结构化段落的信息以「- [段落名] 事实」格式额外输出，"
            "放在整理结果末尾的「## 🔄 建议合并」段\n"
            "6. 如果信息量很少或无需整理，直接返回原始内容\n\n"
            "## 输出格式\n"
            "直接输出整理后的记忆内容（取代整个 AUTO-MEMORY），"
            "每条以「- 」开头（保持 bullet list 格式）。"
            "末尾可选「## 🔄 建议合并」段。"
        )

        consolidated = await self.llm.consolidate_memories(consolidate_prompt)

        if not consolidated or consolidated.strip() in ("", "无", "None"):
            logger.debug("Tier 2.5: LLM 返回空，跳过整理")
            return False

        merge_section = ""
        if "## 🔄 建议合并" in consolidated:
            parts = consolidated.split("## 🔄 建议合并", 1)
            consolidated = parts[0].strip()
            merge_section = parts[1].strip() if len(parts) > 1 else ""

        self.notebook.write_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END, consolidated, mode="replace"
        )

        if merge_section:
            self.notebook.write_merge_suggestions(merge_section)

        logger.info(
            f"Tier 2.5 记忆整理完成: {len(existing)} → {len(consolidated)} 字符"
        )
        return True

    # ── 已有记忆摘要 ──

    def _build_existing_memory_summary(self) -> str:
        """构建已有记忆的结构化摘要，供 LLM 去重使用。"""
        existing = self.notebook.read_section(
            AUTO_MEMORY_START, AUTO_MEMORY_END
        )
        if not existing.strip():
            return ""

        bullets = [l.strip() for l in existing.split("\n")
                   if l.strip().startswith("- ")]
        if not bullets:
            return ""

        summaries = []
        for i, bullet in enumerate(bullets):
            short = bullet[:80].strip()
            summaries.append(f"[{i}] {short}")

        logger.debug(f"已有记忆摘要: {len(bullets)} 条 → {len(summaries)} 条摘要")
        return "\n".join(summaries)

    # ── LLM 记忆提取 ──

    async def _extract_memories(
        self, messages: list[dict[str, Any]], extra_context: str = ""
    ) -> str:
        """调用 LLM 从对话中提取值得长期记住的信息。"""
        existing_summary = ""
        memory_summary = self._build_existing_memory_summary()
        if memory_summary:
            existing_summary = (
                f"\n\n# ⚠️ 已有的记忆记录（请勿重复提取以下信息）\n"
                f"以下是从历史对话中已经提取过的记忆摘要（覆盖全部已有记忆），"
                f"如果本轮对话中的信息与以下内容重复或仅有细微差异，"
                f"请跳过，不要重复输出：\n"
                f"{memory_summary}"
            )

        transcript_parts = []
        for m in messages:
            role_label = m.get('source', m['role'])
            content = m.get('content', '')
            truncated = self._truncate_by_importance(content, tier=2)
            transcript_parts.append(f"[{role_label}] {truncated}")
        transcript = "\n".join(transcript_parts)

        full_input = transcript
        extra_parts = []
        if extra_context:
            extra_parts.append(f"# 额外的历史上下文\n{extra_context}")
        if existing_summary:
            extra_parts.append(existing_summary)
        if extra_parts:
            full_input = (
                "\n\n".join(extra_parts) + f"\n\n# 本轮对话\n{transcript}"
            )

        known_sections = [
            "主人信息", "安绪的信息", "辉夜姬MAD项目",
            "AE插件开发", "舞萌", "已配置的服务",
            "最重要的记忆", "主人的规则",
        ]

        extraction_prompt = (
            "从以下本轮对话中，提取关于「主人」和「安绪与主人的关系」的"
            "**新增**重要记忆。\n\n"
            "⚠️ 重要规则：\n"
            "1. 只提取本轮对话中**首次出现**的新信息，"
            "不要重复上面「已有的记忆记录」中已经存在的内容\n"
            "2. 如果某个信息在已有记忆中以不同措辞出现过，跳过它\n"
            "3. 只提取值得长期记住的信息：主人的新偏好、新习惯、新经历、"
            "新计划、情绪变化、新约定等\n"
            "4. 忽略日常寒暄、测试性对话、临时性内容\n"
            "5. 🔄 记忆更新：如果新事实是对已有记忆的更新/补充/纠正，"
            "请输出更新后的完整版本，并在末尾注明 <!-- 更新自 [序号] -->\n\n"
            "📋 输出格式要求（严格遵循）：\n"
            "- 普通记忆：以「- 」开头，写入 AUTO-MEMORY 列表\n"
            "- **需要合并到上方结构化段落的重大事实**：以「- [段落名] 」开头\n"
            f"  可用的段落名：{' / '.join(known_sections)}\n"
            "  🤖 AUTO 标记：如果你**非常确信**某条事实是对已有段落的明确更新，"
            "用「- [段落名] [AUTO] 事实」格式\n"
            "6. 如果本轮对话没有任何新增内容，只回复「无」\n\n"
            "📋 记忆粒度要求：\n"
            "1. 保留具体的数字、日期、地名、人名\n"
            "2. 保留情感线索\n"
            "3. 保留决策过程\n"
            "4. 技术细节完整保留\n"
            "5. 简短寒暄和纯功能性对话不需要记录"
            f"\n\n{full_input}"
        )

        return await self.llm.extract_memories(extraction_prompt)

    # ── 启动时合并紧急保存 ──

    def _merge_emergency_on_startup(self) -> None:
        """启动时检测 EMERGENCY 段，若有过多的旧转储则立即修剪。"""
        if not self.notebook.enabled:
            return

        section = self.notebook.read_section(EMERGENCY_START, EMERGENCY_END)
        if not section.strip():
            return

        # 检测是否有超过 MAX_EMERGENCY_DUMPS 的旧转储
        header_pattern = re.compile(
            r'<!-- \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC 异常退出前紧急保存 -->'
        )
        dump_count = len(header_pattern.findall(section))

        if dump_count > 3:  # MAX_EMERGENCY_DUMPS
            self.notebook.trim_emergency_section()
            logger.info(
                f"启动时修剪 EMERGENCY: {dump_count} → 最多保留 3 次转储"
            )
