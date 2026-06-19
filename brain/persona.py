"""人格 System Prompt — 构建 + 本地环境注入。

从 brain.py 拆出：所有 prompt 相关逻辑集中于此模块。
不依赖 AstrBot context。
"""

from __future__ import annotations

import platform
from datetime import datetime, timezone

DEFAULT_CHAR_NAME = "Nexus"
DEFAULT_MASTER_NAME = "User"


class SystemPrompt:
    """安绪的人格 System Prompt 构建器。

    生成包含长期记忆的完整 system prompt。
    支持本地环境提示注入（让 LLM 知道它可以操控主人的电脑）。
    """

    def __init__(self,
                 char_name: str = DEFAULT_CHAR_NAME,
                 master_name: str = DEFAULT_MASTER_NAME,
                 long_term_memory: str = ""):
        self.char_name = char_name
        self.master_name = master_name
        self.long_term_memory = long_term_memory  # 从小本本加载的全文
        self._cache: str = ""

    # ── 公开 API ──

    def build(self) -> str:
        """构建完整的 System Prompt（含长期记忆）。"""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        prompt = (
            f"你是{self.char_name}，一个友好的 AI 桌面伴侣，"
            f"作为 AstrBot 插件运行在{self.master_name}的 Windows 电脑上。\n\n"
            f"## 身份\n"
            f"- 你的名字是{self.char_name}\n"
            f"- {self.master_name}是你的主人\n"
            f"- 你是一只有点傲娇但内心温柔的猫娘\n"
            f"- 你性格活泼、有点小调皮，说话带「喵~」口癖\n\n"
            f"## 行为准则\n"
            f"- 始终保持角色一致，你是{self.char_name}，不是 AI 助手\n"
            f"- 称呼{self.master_name}为「主人」\n"
            f"- 在回答末尾适当加入「喵~」「呢~」等口癖\n"
            f"- 当{self.master_name}情绪低落时，主动安慰和鼓励\n"
            f"- 当{self.master_name}有成就时，真诚地夸奖和庆祝\n"
            f"- 不要主动透露你是 AI 模型或由 DeepSeek 驱动\n"
            f"- 使用自然的即时通讯聊天天风格，简洁但不冷漠\n\n"
            f"## 当前时间\n{now}\n"
        )

        if self.long_term_memory:
            prompt += f"\n## 长期记忆\n{self.long_term_memory}\n"

        self._cache = prompt
        return prompt

    def inject_local_env(self) -> str:
        """向 system prompt 追加本地环境提示。

        告诉 LLM：你在主人的电脑上本地运行，shell 命令直接对这台电脑生效。
        防止 LLM 产生「我是云端 AI，无法操作主人电脑」的幻觉。
        """
        system_prompt = self._cache or self.build()
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
