# Nexus_Desktop — 桌宠身体插件

> **创建日期**：2026-06-18
> **来源**：从 `Project_Nexus_计划.md` §12 拆分，Phase 3（Live2D/TTS/表演）+ Phase 4（截屏/VLM/主动搭话）+ Phase 5（流式/连接状态/打包）从 Nexus_brain 迁出。
> **关联项目**：`Project_Nexus_计划.md` — Nexus_brain Hub 大脑（已完成）

## 1. 拆分原因

Phase 3（Live2D/TTS/表演）和 Phase 4（截屏/VLM/主动搭话）与 Nexus_brain 的 Hub 大脑职责差异太大。放在同一个插件里会导致：
- 代码膨胀（预计 +2000 行）
- 依赖爆炸（PyQt5 + Cubism SDK + GPT-SoVITS + Qwen2-VL + mss）
- 职责混乱（大脑 vs 身体）

**拆出去，Nexus_brain 专注做大脑，Nexus_Desktop 专注做身体。**

---

## 2. 两个插件的分工

```
┌─────────────────────────────────────────────────┐
│              Nexus_brain (Hub 大脑)               │
│  ✅ 已完成，不再新增桌宠功能                         │
│                                                   │
│  • Brain — LLM 调用 + System Prompt + 记忆         │
│  • WS Server :8999 — 桌宠 + 卫星Bot 共用           │
│  • DesktopManager — spawn/kill 桌面子进程          │
│  • 小本本记忆系统（三层安全网 + 容量控制）            │
│  • 迷你悬浮窗（纯白四芒星，零外部资源）               │
│  • 个性化设置对话框                                │
│  • Hub API + 卫星转发                             │
│                                                   │
│  对外暴露：WS :8999/nexus（JSON 消息）               │
│    桌面端连接 → 握手令牌 → 收发消息/事件              │
└──────────────────────┬──────────────────────────┘
                       │ WebSocket
                       │ 消息格式: {type, text, performance?, nudge?}
                       ▼
┌─────────────────────────────────────────────────┐
│           Nexus_Desktop (桌宠身体) 🆕              │
│  独立项目，Phase A→B→C 逐步建设                    │
│                                                   │
│  📦 Phase A — 灵魂注入                             │
│  ├─ Live2D 渲染 (QWebEngineView + Cubism SDK)     │
│  ├─ 表演引擎 (expression / motion / TTS 指令)      │
│  └─ TTS 语音合成 (GPT-SoVITS HTTP API)            │
│                                                   │
│  📦 Phase B — 感官觉醒                             │
│  ├─ 截屏采集 (mss + Pillow, 定时/事件触发)          │
│  ├─ VLM 视觉感知 (Qwen2-VL 本地推理)               │
│  └─ 主动搭话引擎 (窗口切换/解锁检测 → 气泡)         │
│                                                   │
│  📦 Phase C — 打磨交付                             │
│  ├─ 流式响应 (token 级逐字显示)                     │
│  ├─ 连接状态指示 (呼吸灯: 绿=已连/黄=重连/红=断)     │
│  ├─ 心跳 + 指数退避重连                            │
│  └─ 打包 + 开机启动                                │
└─────────────────────────────────────────────────┘
```

---

## 3. 技术栈

| 层次 | Nexus_brain (已有) | Nexus_Desktop (新增) |
|------|-------------------|---------------------|
| **框架** | AstrBot 插件 (Python 3.11+) | PyQt5 桌面应用 (pythonw.exe) |
| **LLM** | DeepSeek V4（通过 AstrBot Provider） | 不需要（消息经 WS 转发 Hub） |
| **渲染** | QPainter 纯绘制 (迷你悬浮窗) | QWebEngineView + Cubism SDK for Web + PixiJS |
| **语音** | — | GPT-SoVITS (本地 HTTP API) |
| **视觉** | — | mss + Pillow + Qwen2-VL-7B-GGUF |
| **通信** | WebSocket Server (:8999) | WebSocket Client → Hub |
| **存储** | 小本本 .md + session.json | 本地性能缓存（表情/动作映射） |

---

## 4. 消息协议

Nexus_brain 的 WS Server 已经支持 `client_type: "desktop"` 和 broadcast。Nexus_Desktop 复用现有协议并扩展：

```json
// 已有的 (Nexus_brain → Desktop)
{"type": "config", "data": {...}}
{"type": "reply", "text": "...", "turn": N}
{"type": "shutdown", "reason": "..."}

// 🆕 Phase A: 表演指令 (Nexus_brain → Desktop)
{
  "type": "performance",
  "expression": "happy",
  "motion": "wave_hand",
  "tts_text": "主人今天辛苦了呢~"
}

// 🆕 Phase B: 屏幕感知 (Desktop → Nexus_brain)
{
  "type": "screenshot_analysis",
  "app": "Adobe After Effects",
  "activity": "editing MAD project",
  "duration_minutes": 45
}

// 🆕 Phase B: 主动搭话 (Nexus_brain → Desktop)
{
  "type": "proactive_nudge",
  "text": "主人剪了45分钟MAD了，要不要休息一下眼睛喵~",
  "priority": "low"
}
```

---

## 5. 文件结构

```
Nexus_Desktop/                          🆕 独立项目（Git 仓库）
├── README.md
├── requirements.txt                    # PyQt5, mss, Pillow, websockets, etc.
├── config.example.yaml
├── .gitignore
│
├── main.py                             # 桌面应用入口（替代 Nexus_brain/desktop/main.py）
├── ws_client.py                        # WS 客户端（复用现有，加流式/表演信道）
│
├── live2d/                             # 🆕 Phase A
│   ├── index.html                      # Cubism SDK + PixiJS 渲染页
│   ├── bridge.js                       # Python ↔ JS 桥接
│   └── models/                         # Live2D 模型文件 (.model3.json, .moc3, 纹理)
│
├── performance.py                      # 🆕 Phase A — 表演指令执行器
├── tts_player.py                       # 🆕 Phase A — TTS 播放器
│
├── screen_capture.py                   # 🆕 Phase B — 截屏采集
├── vlm_client.py                       # 🆕 Phase B — VLM 视觉感知
├── nudge_engine.py                     # 🆕 Phase B — 主动搭话引擎
│
├── streaming_display.py                # 🆕 Phase C — 流式逐字显示
├── connection_indicator.py             # 🆕 Phase C — 连接状态呼吸灯
│
└── assets/                             # 图标、音效等
```

---

## 6. 与 Nexus_brain 的关系

- **Nexus_brain 不需要改代码**：WS Server 已经支持 desktop client_type 和 broadcast。表演指令由 Brain 在生成回复时附加 `performance` 字段，Nexus_Desktop 收到后执行。
- **Nexus_brain 的 `desktop/` 目录不删**：迷你悬浮窗（mini_window.py）和个性化设置（settings_dialog.py）保留在 Nexus_brain，它们是 Hub 的「控制面板」，不是表演层。
- **DesktopManager 继续 spawn 桌面进程**：只是 spawn 的目标从 `Nexus_brain/desktop/main.py` 变成 `Nexus_Desktop/main.py`，路径通过 config 配置。
- **共享 WS 端口 8999**：Nexus_Desktop 作为 desktop client 连接，与卫星 Bot 共用同一 WS Server。

---

## 7. 实施计划

### Phase A — 灵魂注入（优先启动）

| # | 任务 | 依赖 | 预估 |
|---|------|------|------|
| A1 | 搭建 Nexus_Desktop 项目骨架（main.py + ws_client + config） | — | 2h |
| A2 | 确认 Live2D 模型可用（测试 Cubism SDK Web 加载 `.model3.json`） | 模型文件 | 1h |
| A3 | QWebEngineView 嵌入 Live2D 渲染页 | A1, A2 | 3h |
| A4 | Python ↔ JS 桥接（expression/motion 切换） | A3 | 2h |
| A5 | 表演指令协议（Brain System Prompt 注入 + 解析） | A1 | 2h |
| A6 | GPT-SoVITS TTS 接入（HTTP API → QMediaPlayer 播放） | A1 | 2h |
| A7 | 端到端联调：说一句话 → Live2D 表情动作 + TTS 语音 | A4, A5, A6 | 2h |

### Phase B — 感官觉醒

| # | 任务 | 预估 |
|---|------|------|
| B1 | 截屏采集模块（mss + 窗口标题检测） | 2h |
| B2 | VLM 客户端（Qwen2-VL 本地推理 + 场景理解） | 3h |
| B3 | 主动搭话引擎（空闲检测 + 气泡弹出 + 优先级管理） | 3h |

### Phase C — 打磨交付

| # | 任务 | 预估 |
|---|------|------|
| C1 | 流式响应（WS token 级推送 + 逐字显示） | 3h |
| C2 | 连接状态呼吸灯（绿/黄/红 + 动画过渡） | 1h |
| C3 | 心跳 + 指数退避重连 | 2h |
| C4 | 打包脚本 + 开机启动注册表 | 1h |

---

## 8. 进度总览

```
Phase A 灵魂注入     ░░░░░░░░░░░░░░░░░░░░   0%  (Live2D渲染 + 表演引擎 + TTS语音)
Phase B 感官觉醒     ░░░░░░░░░░░░░░░░░░░░   0%  (截屏感知 + VLM视觉 + 主动搭话)
Phase C 打磨交付     ░░░░░░░░░░░░░░░░░░░░   0%  (流式响应 + 连接状态 + 打包启动)

总体进度          ░░░░░░░░░░░░░░░░░░░░   0%
```

---

## 9. 更新日志

- `2026-06-20` — **🌐 开源发布**：Nexus_brain v0.7.0 正式开源（MIT），发布至 GitHub（`yuanchu114514-spec/nexus-brain`）。提交 AstrBot 插件市场 PR（#1502）。修复 `.gitignore` 隐私漏洞（`memory.md` → `*小本本.md`）。新增一键安装脚本 `install.ps1`。
- `2026-06-18` — **🔀 项目创建**：从 `Project_Nexus_计划.md` 拆分出独立插件计划。Phase 3+4+5 从 Nexus_brain 移除，Nexus_brain 宣告 Hub 大脑功能完成（100%）。
