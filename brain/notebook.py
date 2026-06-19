"""小本本 Markdown 文件 I/O — 段读写、容量控制、自动合并。

从 brain.py 拆出：所有文件系统操作集中于此模块。
不依赖 AstrBot context 或其他 brain 子模块。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from astrbot.api import logger

# ═══════════════════════════════════════════════════════════
# 标记常量
# ═══════════════════════════════════════════════════════════
AUTO_MEMORY_START = "<!-- AUTO-MEMORY-START -->"
AUTO_MEMORY_END = "<!-- AUTO-MEMORY-END -->"
CHECKPOINT_START = "<!-- CHECKPOINT-START -->"
CHECKPOINT_END = "<!-- CHECKPOINT-END -->"
EMERGENCY_START = "<!-- EMERGENCY-SAVE-START -->"
EMERGENCY_END = "<!-- EMERGENCY-SAVE-END -->"
MERGE_START = "<!-- AI-MERGE-START -->"
MERGE_END = "<!-- AI-MERGE-END -->"

# ═══════════════════════════════════════════════════════════
# 容量控制常量
# ═══════════════════════════════════════════════════════════
MAX_EMERGENCY_DUMPS = 3
AUTO_MEMORY_MAX_BULLETS = 60
AUTO_MEMORY_MAX_CHARS = 12000

# ═══════════════════════════════════════════════════════════
# 正则模式
# ═══════════════════════════════════════════════════════════
_EMERGENCY_HEADER_RE = re.compile(
    r'<!-- \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC 异常退出前紧急保存 -->'
)
_AUTO_MEMORY_BLOCK_RE = re.compile(
    r'<!-- \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC -->'
)


class NotebookIO:
    """小本本 Markdown 文件读写管理器。

    管理四个标记段（AUTO-MEMORY / CHECKPOINT / EMERGENCY / MERGE）
    以及容量控制（紧急保存修剪、自动记忆修剪）。

    纯文件 I/O 层 — 不含业务逻辑。
    """

    def __init__(self,
                 memory_enabled: bool = False,
                 memory_path: Path | None = None,
                 char_name: str = "Nexus"):
        self._enabled = memory_enabled
        self._path = memory_path
        self.char_name = char_name

    # ── 属性 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def path(self) -> Path | None:
        return self._path

    @path.setter
    def path(self, value: Path | None) -> None:
        self._path = value

    @property
    def folder(self) -> str:
        """记忆文件夹路径（字符串形式，供 main.py 读取）。"""
        return str(self._path.parent) if self._path else ""

    # ── 加载 ──

    def load_memory(self) -> str:
        """读取小本本全文，作为长期记忆注入 System Prompt。"""
        if not self._enabled or self._path is None or not self._path.exists():
            if self._enabled:
                logger.info(f"小本本不存在: {self._path}，长期记忆为空")
            return ""
        try:
            content = self._path.read_text(encoding="utf-8").strip()
            if content:
                logger.info(f"长期记忆已加载: {len(content)} 字符 ({self._path})")
            return content
        except OSError as e:
            logger.warning(f"读取小本本失败 ({self._path}): {e}")
            return ""

    # ── 段管理: ensure ──

    def ensure_section(self, start_marker: str, end_marker: str,
                       section_title: str = "") -> None:
        """确保小本本中存在指定标记段，不存在则创建。"""
        if not self._enabled or self._path is None:
            return
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            header = f"# 🐱 {self.char_name}的小本本\n\n"
            if section_title:
                header += f"{section_title}\n"
            header += f"{start_marker}\n{end_marker}\n"
            self._path.write_text(header, encoding="utf-8")
            logger.info(f"小本本已创建: {self._path}")
            return

        content = self._path.read_text(encoding="utf-8")
        has_start = start_marker in content
        has_end = end_marker in content
        if has_start and has_end:
            return

        if not has_start and not has_end:
            new_block = ""
            if section_title:
                new_block += f"{section_title}\n"
            new_block += f"{start_marker}\n{end_marker}\n"
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"\n{new_block}"
        elif not has_start:
            end_idx = content.find(end_marker)
            content = content[:end_idx] + f"{start_marker}\n" + content[end_idx:]
        else:
            start_idx = content.find(start_marker) + len(start_marker)
            content = content[:start_idx] + f"\n{end_marker}\n" + content[start_idx:]

        self._path.write_text(content, encoding="utf-8")

    # ── 段管理: write ──

    def write_section(self, start_marker: str, end_marker: str, text: str,
                      mode: str = "append", section_title: str = "") -> None:
        """向标记段写入内容。

        Args:
            mode: "append" 在 END 标记前插入；"replace" 替换整个段内容。
        """
        if not self._enabled or self._path is None:
            logger.debug("记忆系统未启用，跳过写入")
            return

        self.ensure_section(start_marker, end_marker, section_title)
        content = self._path.read_text(encoding="utf-8")

        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            logger.warning(f"小本本标记异常 ({start_marker})，跳过写入")
            return

        if mode == "replace":
            before = content[:start_idx + len(start_marker)]
            after = content[end_idx:]
            new_content = before + "\n" + text + "\n" + after
        else:
            insert_idx = end_idx
            new_content = (
                content[:insert_idx] + text + content[insert_idx:]
            )

        try:
            self._path.write_text(new_content, encoding="utf-8")
        except OSError as e:
            logger.warning(f"写入小本本失败: {e}")

    # ── 段管理: read ──

    def read_section(self, start_marker: str, end_marker: str) -> str:
        """读取标记段之间的内容（不含标记本身）。"""
        if not self._enabled or self._path is None or not self._path.exists():
            return ""
        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError:
            return ""

        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            return ""

        section = content[start_idx + len(start_marker):end_idx].strip()
        return section

    # ── 段管理: clear ──

    def clear_section(self, start_marker: str, end_marker: str) -> None:
        """清空标记段的内容。"""
        if not self._enabled or self._path is None or not self._path.exists():
            return
        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError:
            return

        start_idx = content.find(start_marker)
        end_idx = content.find(end_marker)

        if start_idx == -1 or end_idx == -1:
            return

        before = content[:start_idx + len(start_marker)]
        after = content[end_idx:]
        new_content = before + "\n" + after
        self._path.write_text(new_content, encoding="utf-8")

    # ── 容量控制 ──

    def trim_emergency_section(self) -> int:
        """修剪 EMERGENCY 段：只保留最近 MAX_EMERGENCY_DUMPS 次紧急转储。

        Returns:
            删除的转储次数。
        """
        section = self.read_section(EMERGENCY_START, EMERGENCY_END)
        if not section.strip():
            return 0

        dumps = _EMERGENCY_HEADER_RE.split(section)
        dumps = [d.strip() for d in dumps if d.strip()]

        if len(dumps) <= MAX_EMERGENCY_DUMPS:
            return 0

        removed = len(dumps) - MAX_EMERGENCY_DUMPS
        kept = dumps[-MAX_EMERGENCY_DUMPS:]

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_content = ""
        for dump in kept:
            new_content += f"<!-- {timestamp} 异常退出前紧急保存 -->\n{dump}\n"

        self.write_section(EMERGENCY_START, EMERGENCY_END, new_content.strip(),
                           mode="replace")
        logger.info(f"EMERGENCY 段修剪: {removed} 次旧转储已删除，保留 {len(kept)} 次")
        return removed

    def trim_auto_memory(self) -> int:
        """修剪 AUTO-MEMORY 段：限制 bullet 数量和总字符数。

        bullet 超 AUTO_MEMORY_MAX_BULLETS 或总字符超 AUTO_MEMORY_MAX_CHARS
        时，按时间戳块从旧到新逐块移除。

        Returns:
            移除的 bullet 数量。
        """
        section = self.read_section(AUTO_MEMORY_START, AUTO_MEMORY_END)
        if not section.strip():
            return 0

        # 按时间戳块拆分
        blocks = _AUTO_MEMORY_BLOCK_RE.split(section)
        structured_blocks: list[tuple[str, list[str]]] = []
        current_header = ""
        for part in blocks:
            part = part.strip()
            if not part:
                continue
            if _AUTO_MEMORY_BLOCK_RE.match(f"<!-- {part} -->"):
                # This won't match exactly — instead detect headers by pattern
                pass
            if part.startswith("<!-- ") and "UTC -->" in part:
                current_header = part
            else:
                bullets = [l.strip() for l in part.split("\n")
                          if l.strip().startswith("- ")]
                if bullets:
                    structured_blocks.append((current_header, bullets))
                current_header = ""

        if not structured_blocks:
            return 0

        total_bullets = sum(len(b[1]) for b in structured_blocks)
        total_chars = sum(
            len("\n".join(b[1])) + len(b[0]) for b in structured_blocks
        )

        if (total_bullets <= AUTO_MEMORY_MAX_BULLETS and
                total_chars <= AUTO_MEMORY_MAX_CHARS):
            return 0

        removed = 0
        while structured_blocks and (
            total_bullets > AUTO_MEMORY_MAX_BULLETS or
            total_chars > AUTO_MEMORY_MAX_CHARS
        ):
            old_header, old_bullets = structured_blocks[0]
            total_bullets -= len(old_bullets)
            total_chars -= len("\n".join(old_bullets)) + len(old_header)
            removed += len(old_bullets)
            structured_blocks = structured_blocks[1:]

        if removed == 0:
            return 0

        new_section = ""
        for header, bullets in structured_blocks:
            new_section += f"{header}\n" + "\n".join(bullets) + "\n\n"

        self.write_section(AUTO_MEMORY_START, AUTO_MEMORY_END,
                           new_section.strip(), mode="replace")
        logger.info(
            f"AUTO-MEMORY 修剪: {removed} 条旧 bullet 已移除，"
            f"保留 {total_bullets}/{total_chars} 字符"
        )
        return removed

    def run_maintenance(self) -> None:
        """统一维护入口：修剪 EMERGENCY + AUTO-MEMORY。"""
        self.trim_emergency_section()
        self.trim_auto_memory()

    # ── 辅助 ──

    def write_merge_suggestions(self, text: str) -> None:
        """将 AI 建议合并的重要事实写入小本本 AI-MERGE 段。"""
        if not text.strip():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        block = f"\n<!-- {timestamp} -->\n{text}\n"
        self.write_section(MERGE_START, MERGE_END, block, mode="append",
                           section_title="🔄 AI 建议合并到上方段落")
        logger.info(f"AI 合并建议已写入小本本 MERGE 段: {len(text)} 字符")

    def auto_merge_to_section(self, section_name: str, fact: str) -> bool:
        """将高置信度记忆直接合并到小本本上方的结构化段落。

        在目标段落末尾追加新事实。仅当目标段落确实存在时才合并。
        """
        if not self._enabled or self._path is None:
            return False
        if not self._path.exists():
            logger.debug(f"AUTO-MERGE: 小本本不存在，跳过 {section_name}")
            return False

        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError:
            return False

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

        section_start = match.end()
        next_section = re.search(r'\n## ', content[section_start:])
        if next_section:
            insert_pos = section_start + next_section.start()
        else:
            auto_mem_pos = content.find("## 🤖 自动提取的记忆", section_start)
            if auto_mem_pos > 0:
                insert_pos = auto_mem_pos
            else:
                insert_pos = len(content)

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        new_line = f"\n- {fact}  <!-- [AUTO] {timestamp} -->\n"
        new_content = content[:insert_pos].rstrip() + new_line + content[insert_pos:]

        try:
            self._path.write_text(new_content, encoding="utf-8")
            logger.info(f"AUTO-MERGE ✅: [{section_name}] {fact[:60]}...")
            return True
        except OSError as e:
            logger.warning(f"AUTO-MERGE 写入失败: {e}")
            return False
