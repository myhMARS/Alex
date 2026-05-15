# Alex Agent 架构设计文档

## 概述

Alex 是一个支持工具调用、流式输出的对话式 AI Agent 智能体。核心能力：

- **工具调用**：动态注册/注销工具，Agent 自主决策调用时机
- **流式输出**：token 级别流式响应，支持 thinking 内容实时展示
- **上下文管理**：抽象记忆接口，可扩展接入外部记忆框架
- **模型适配**：工厂模式统一适配多平台 LLM（DeepSeek thinking mode 支持）
- **自适应成长**：从历史对话中自主提炼技能，持续进化
- **TUI 交互**：基于 Textual 的终端界面，支持滚动、折叠、会话持久化

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                           main.py (入口)                             │
│          TUI (Textual) / Single Query / Streaming CLI                │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
                    ▼                     ▼
          ┌──────────────┐      ┌──────────────────┐
          │  alex/tui.py │      │  Simple CLI      │
          │  (Textual    │      │  (Rich Console)  │
          │   App)       │      │                  │
          └──────┬───────┘      └────────┬─────────┘
                 │                       │
                 └───────────┬───────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Agent (核心编排层)                            │
│                                                                     │
│  ┌────────────┐ ┌──────────────┐ ┌────────────┐ ┌───────────────┐  │
│  │ToolRegistry│ │StreamHandler │ │SkillManager│ │ AgentGraph    │  │
│  └────────────┘ └──────────────┘ └────────────┘ │ (LangGraph)   │  │
│                                                  └───────────────┘  │
└───┬──────────────────┬──────────────────┬──────────────────┬────────┘
    │                  │                  │                  │
    ▼                  ▼                  ▼                  ▼
┌────────┐     ┌────────────┐     ┌────────────┐     ┌───────────┐
│ Tools  │     │   Memory   │     │   Skills   │     │ LLM       │
│ Layer  │     │   Layer    │     │   Layer    │     │ Factory   │
└────────┘     └────────────┘     └────────────┘     └───────────┘
```

---

## 模块文档索引

| 模块 | 文档 | 摘要 |
|------|------|------|
| Agent 核心编排 | [agent.md](./agent.md) | 系统编排中心，协调 LLM、Memory、Tools、Skills、Streaming |
| TUI 交互界面 | [display.md](./display.md) | Textual TUI 应用，支持滚动、折叠、会话管理 |
| LLM 工厂层 | [llm.md](./llm.md) | 工厂模式 + 装饰器注册，统一适配多平台 LLM |
| 记忆管理层 | [memory.md](./memory.md) | 抽象记忆接口，支持缓冲记忆及未来 RAG 扩展 |
| 流式输出 | [streaming.md](./streaming.md) | 基于 LangGraph astream_events 的流式事件分发 |
| 自适应技能系统 | [skills.md](./skills.md) | 技能提炼、检索、进化的完整生命周期管理 |
| 配置管理 | [config.md](./config.md) | 多 provider 配置加载，支持文件和环境变量 |

---

## 项目结构

```
alex/
├── __init__.py
├── agent.py              # Agent 核心编排 + ChatResponse
├── config.py             # 配置加载
├── callbacks.py          # LangChain 回调 → 事件桥接
├── display.py            # Rich 渲染工具（非 TUI 模式使用）
├── tui.py                # Textual TUI 应用（交互模式）
├── llm/                  # LLM 工厂层
├── memory/               # 记忆管理层
├── skills/               # 自适应技能系统
├── tools/                # 工具层（web_search, web_fetch）
└── streaming/            # 流式事件定义
```

---

## 后续演进方向

1. **多 Agent 协作** — 基于 LangGraph 的 multi-agent 模式
2. **持久化记忆** — 接入 Redis / PostgreSQL 存储对话历史
3. **RAG 增强** — Memory 层集成向量检索
4. **Web API** — FastAPI + SSE/WebSocket 暴露流式接口
5. **可观测性** — 集成 LangSmith / OpenTelemetry 追踪
6. **技能向量化检索** — SkillRetriever 升级为 embedding 语义匹配
7. **跨用户技能共享** — 多用户场景下的技能市场/共享池
8. **技能组合编排** — 多技能协同处理复杂场景
9. **TUI 增强** — 多窗格布局、图片预览、文件拖放
