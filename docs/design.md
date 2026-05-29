# Alex Agent 架构设计文档

## 概述

Alex 是一个支持工具调用、流式输出的对话式 AI Agent 智能体。核心能力：

- **工具调用**：动态注册/注销工具，统一权限策略与审计日志
- **本地能力**：`read` / `write` / `edit` / `grep` / `glob` / `git_inspect` / `bash` / `pwsh` 让 Alex 真的能在终端"动手"
- **副作用确认**：写文件 / 跑 shell 弹 modal 并展示 diff / argv，用户确认后才执行；每次决策追加到审计日志
- **MCP & 用户插件**：自动发现 `~/.alex/mcp.json` 中的 MCP server（支持 stdio 与 HTTP transport）与 `~/.alex/plugins/*.py` 中的自定义工具
- **流式输出**：token 级别流式响应，支持 thinking 内容实时展示
- **Markdown 渲染**：终态回复自动以 Rich Markdown 渲染（代码块/列表/加粗/内联代码），流式期间保持纯文本以避免布局抖动
- **上下文管理**：抽象记忆接口，可扩展接入外部记忆框架
- **模型适配**：工厂模式统一适配多平台 LLM（DeepSeek thinking mode 支持）
- **自适应成长**：从历史对话中自主提炼技能，持续进化
- **TUI 交互**：基于 Textual 的终端界面，支持滚动、折叠、会话持久化、modal 确认
- **Cron 后台任务**：APScheduler 驱动的 prompt 驱动后台任务调度，支持 durable 任务定义持久化与恢复到当前会话，结果以 cron 流式对话注入 TUI
- **事件总线**：`AsyncEventBus` 统一跨模块通信，替代轮询式通知队列

---

## 最终业务目标

Alex 的最终目标，不只是做一个"能聊天、能调用工具"的终端 AI，而是成为一个**常驻终端的个人智能工作代理**。

它在业务层面的终局形态可以概括为：

1. **从一次性问答升级为长期协作**
   用户不是临时提一个问题然后离开，而是把 Alex 当成持续协作的工作助手。它需要理解上下文、延续会话、恢复历史，并在多个工作回合中保持连贯。

2. **从被动响应升级为主动服务**
   用户不只是在需要时询问，还会把"定期搜索、跟踪、抓取、汇总、提醒"这类任务交给 Alex 后台执行。Alex 要能够把一次性的请求转化成持续运行的任务，并把结果主动回流到当前工作流中。

3. **从即时能力升级为经验沉淀**
   项目的核心差异化目标不是单次回答质量，而是"越用越懂用户"。Alex 要从历史对话中提炼稳定的方法论，形成可复用技能，并在未来类似任务中优先复用这些高成功率策略。

4. **从工具集合升级为个人工作操作系统**
   最终形态不是一个命令行聊天框，而是一个围绕终端工作流构建的智能工作台：能搜索、抓取、执行、跟踪、学习、恢复会话，并逐步承接用户的重复性知识工作。

### 面向用户的核心价值

从业务视角看，Alex 最终要解决的是以下几类问题：

- **减少重复劳动**：把"搜资料、看网页、摘重点、隔一段时间再查一次"的重复流程自动化
- **减少上下文切换**：用户不需要频繁在终端、浏览器、笔记和任务系统之间来回跳转
- **建立长期可用性**：与普通聊天式 AI 不同，Alex 的价值会随着使用次数增加而上升
- **形成个人工作闭环**：`提问 -> 调工具 -> 获得结果 -> 用户反馈 -> 提炼技能 -> 下次更好`

### 最终产品定位

如果用一句更产品化的话来定义：

> Alex 的最终目标，是成为一个在终端里持续陪用户工作的个人 AI Agent：既能即时回答，也能后台执行；既能调用工具，也能沉淀方法；既能服务当前任务，也能随着长期使用不断进化。

### 对应的阶段性业务演进

这个业务目标可以分成四个阶段理解：

1. **终端原生 AI 助手**
   能在 TUI 中完成对话、工具调用、流式展示和会话恢复。

2. **可订阅的后台代理**
   能通过 cron 持续跟踪用户关心的信息和任务，并主动把结果送回来。

3. **会成长的个人智能体**
   能从历史问题解决过程里提炼技能，把有效策略沉淀成稳定能力。

4. **个人智能工作系统**
   能逐步承担更多重复性知识工作，并进一步扩展到 API、多前端、多用户与技能共享场景。

### 对架构设计的直接要求

正因为最终目标不是"会聊天"，而是"长期协作的个人工作代理"，所以架构必须支持：

1. **长期状态管理**：会话、历史、任务、技能都需要稳定建模
2. **后台执行能力**：不能只有前台问答，还要支持持续运行和结果回流
3. **能力持续进化**：技能提炼、反馈闭环、反思机制必须是一等能力
4. **多入口扩展能力**：未来不应只局限于 TUI，应能够扩展到 API、Web、自动化集成场景
5. **可控的副作用**：写文件、跑命令必须经过权限策略 + 用户确认 + 审计日志，让 agent 行为可追溯、可回滚

---

## 系统架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                           main.py (入口)                             │
│                          TUI (Textual)                               │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
                     ┌──────────────────────┐
                     │   alex/tui/          │
                     │  AlexApp             │
                     │  ChatProjector       │
                     │  NotificationCtrl    │
                     │  PermissionConfirm   │
                     │  StreamRenderer      │
                     │  Markdown renderer   │
                     └────────┬─────────────┘
                              │ AgentFacade Protocol
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    Agent 薄 Facade (alex/agent/)                      │
│                                                                     │
│  ┌──────────────┐ ┌─────────────┐ ┌────────────┐ ┌──────────────┐  │
│  │SessionService│ │ CronService │ │ToolRegistry│ │ AgentGraph   │  │
│  ├──────────────┤ ├─────────────┤ │ToolExecutor│ │ (LangGraph)  │  │
│  │TurnOrch'ator │ │CronTurnHand │ │+Permissions│ │              │  │
│  │FeedbackSrv   │ │PromptAssemb │ │            │ │              │  │
│  │SkillAdminSrv │ │             │ │            │ │              │  │
│  └──────────────┘ └─────────────┘ └────────────┘ └──────────────┘  │
└───┬──────────────────┬──────────────────┬──────────────────┬────────┘
    │                  │                  │                  │
    ▼                  ▼                  ▼                  ▼
┌────────┐     ┌────────────┐     ┌────────────┐     ┌───────────┐
│ Tools  │     │   Memory   │     │   Skills   │     │ LLM       │
│ Layer  │     │   Layer    │     │   Layer    │     │ Factory   │
│        │     │            │     │            │     │           │
│ fs/edit│     │ Buffer     │     │ Retrieve   │     │ DeepSeek  │
│ grep   │     │ (sliding)  │     │ Reflect    │     │ OpenAI    │
│ glob   │     │            │     │ Evolve     │     │ Anthropic │
│ bash   │     │            │     │ Merge      │     │           │
│ pwsh   │     │            │     │ Store      │     │           │
│ git_*  │     │            │     │            │     │           │
│ web_*  │     │            │     │            │     │           │
│ cron   │     │            │     │            │     │           │
│ MCP    │     │            │     │            │     │           │
│ plugin │     │            │     │            │     │           │
└────┬───┘     └────────────┘     └────────────┘     └───────────┘
     │
     │  AuditLogger → ~/.alex/audit/permissions.jsonl
     │
     ▼
┌────────────┐                     ┌────────────┐
│ EventBus   │ ◀───── publish ────│  Agent     │
│ (async)    │                     └────────────┘
│            │ ──── subscribe ────▶┌────────────┐
└────────────┘                     │   Store    │
                                   │  Sessions  │
                                   └────────────┘
```

---

## 模块文档索引

| 模块 | 文档 | 摘要 |
|------|------|------|
| Agent 核心编排 | [agent.md](./agent.md) | 薄 facade 编排层，协调 LLM、Memory、Tools、Skills、Cron |
| TUI 交互界面 | [display.md](./display.md) | Textual TUI 应用，支持滚动、折叠、反馈、Markdown 渲染、权限确认 modal |
| 工具生态 | [tools.md](./tools.md) | Registry/Executor + 权限策略 + 审计 + 用户插件 + MCP 客户端 + 各内置工具 |
| LLM 工厂层 | [llm.md](./llm.md) | 工厂模式 + 装饰器注册，统一适配多平台 LLM |
| 记忆管理层 | [memory.md](./memory.md) | 抽象记忆接口，支持缓冲记忆及未来 RAG 扩展 |
| 流式输出 | [streaming.md](./streaming.md) | 基于 LangGraph astream_events 的流式事件分发 |
| 自适应技能系统 | [skills.md](./skills.md) | SkillService + SkillStore 分层，技能提炼/检索/进化 |
| 类型化事件系统 | [events.md](./events.md) | Event -> Command/DomainEvent/UIEvent 三层事件体系 |
| 配置管理 | [config.md](./config.md) | 多 provider 配置加载，支持文件和环境变量 |
| 模块化重构方案 | [refactor-modular-architecture.md](./refactor-modular-architecture.md) | 重构全貌、已完成的里程碑、验收标准 |
| 未来演进路线图 | [roadmap-future-evolution.md](./roadmap-future-evolution.md) | 工具/技能/Cron/可观测性/多入口的下一步规划 |

---

## 项目结构

```
alex/
├── agent/                      # Agent 应用层
│   ├── service.py              # Agent — 薄 facade
│   ├── factory.py              # create_agent() — 装配 + 插件加载
│   ├── chat_service.py         # ChatAppService（聊天流、工具执行、图管理）
│   ├── session_service.py      # SessionService（session 持久化边界）
│   ├── cron_service.py         # CronService（cron 调度）
│   ├── feedback_service.py     # FeedbackAppService
│   ├── skill_admin_service.py  # SkillAdminAppService
│   ├── orchestrator.py         # TurnOrchestrator
│   ├── cron_handler.py         # CronTurnHandler
│   ├── prompt.py               # PromptAssembler
│   └── ports.py                # AgentFacade Protocol
├── bus/                        # 事件总线
│   ├── events.py
│   └── in_memory.py            # AsyncEventBus
├── memory/                     # 记忆层
│   ├── base.py
│   └── buffer.py               # BufferMemory 滑动窗口
├── skill/                      # 自适应技能系统
│   ├── service.py
│   ├── repository.py           # SkillStore JSON
│   ├── matcher.py              # SkillRetriever
│   ├── reflector.py            # LLM 反思
│   └── evolution.py            # 生命周期
├── store/                      # 持久化层
│   ├── session.py
│   ├── session_serializer.py   # BaseMessage <-> dict
│   └── session_adapter.py      # SessionPersistence 事件驱动
├── scheduler/
│   └── manager.py              # CronManager APScheduler
├── tools/                      # 工具层
│   ├── registry.py / executor.py / ports.py
│   ├── permissions.py          # PermissionPolicy + AuditLogger + summariser
│   ├── plugin_loader.py        # 用户插件
│   ├── mcp_client.py           # MCP 多 transport 客户端
│   ├── fs.py                   # read / write / edit + FileReadTracker
│   ├── search.py               # grep / glob
│   ├── shell.py                # bash / pwsh
│   ├── git.py                  # git_inspect
│   ├── time.py / web_search.py / web_fetch.py / cron.py
├── tui/                        # TUI 界面
│   ├── app.py                  # AlexApp 主类
│   ├── controller.py           # 命令分发、session、toggles
│   ├── chat_projector.py       # bus → widget
│   ├── notification_controller.py # toast / feedback / 权限确认
│   ├── confirm_screen.py       # PermissionConfirmScreen modal
│   ├── view_state.py / view_models.py / cron_history.py
│   ├── presenter.py            # AlexBubble / UserBubble / ToolBubble
│   ├── stream_renderer.py
│   └── markdown.py             # render_response — Rich Markdown 渲染层
├── llm/                        # LLM 工厂层
├── prompts/                    # Jinja2 提示词模板
└── config.py
```

---

## 后续演进方向

详见 [roadmap-future-evolution.md](./roadmap-future-evolution.md)。简要：

1. **技能向量化检索** — `SkillRetriever` 升级为 embedding 语义匹配
2. **Cron 任务持久化 + 变化检测** — `~/.alex/cron/jobs.json`，配合 `last_result_hash` 减少噪音
3. **可观测性** — Token 成本看板、OpenTelemetry trace、replay 模式
4. **Web API** — FastAPI + SSE/WebSocket 暴露流式接口
5. **多入口** — VS Code 扩展、headless 模式、`alex serve` daemon
6. **跨用户技能共享** — 多用户场景下的技能市场/共享池
7. **TUI 增强** — 多模态输入、会话搜索、消息编辑/重生成
8. **稳定性** — `SkillStore` / `SessionRepository` 原子写、port contract tests
