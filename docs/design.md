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
- **模型适配**：基于 OpenAI SDK 的统一 ChatClient，支持 DeepSeek / OpenAI / Anthropic 多 provider
- **自适应成长**：从历史对话中自主提炼技能，持续进化
- **TUI 交互**：基于 Textual 的终端界面，支持滚动、折叠、会话持久化、modal 确认
- **Cron 定时任务**：APScheduler 驱动的 prompt 驱动后台任务调度，支持 durable 任务定义持久化与恢复到当前会话，结果以 cron 流式对话注入 TUI
- **Kernel + Module 架构**：共享 kernel 层定义跨模块契约，8 个可插拔模块仅通过 MessageBus 通信，模块间零直接导入

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
6. **模块间零耦合**：每个模块独立演进，互不导入，仅通过 kernel 定义的契约通信

---

## 系统架构

### Kernel + Module 设计

Alex 采用 **共享 kernel + 可插拔模块** 架构。`alex/kernel/` 定义所有跨模块类型（契约、DTO、总线协议），8 个业务模块仅依赖 kernel，模块之间**零直接导入**。

所有通信通过 `AsyncEventBus`（实现 `MessageBus` Protocol）完成，支持三种消息语义：

| 语义 | 类型 | 用途 |
|------|------|------|
| **Event** | 广播 pub/sub | 状态变更通知（`TurnCompleted`, `TokenEmitted`, `ToolStarted`...） |
| **Command** | 点对点，可选 ack | 行为请求（`UserTurnRequested`, `ReflectSkills`, `CronTurnRequested`...） |
| **Request/Reply** | 点对点，有返回值 | 能力调用（`ExecuteTool → ToolResult`, `GetContext → list[dict]`...） |

```mermaid
graph TB
    Entry[entry.py] --> Host

    subgraph Host["ModuleHost"]
        Topo[拓扑排序后按依赖顺序 start(bus)]
    end

    Host --> Bus

    subgraph Bus["MessageBus — Event / Command / Request"]
    end

    Bus --- Agent[AgentModule<br/>deps: memory, tools, skill]
    Bus --- Tools[ToolsModule<br/>deps: none]
    Bus --- Skill[SkillModule<br/>deps: tools]
    Bus --- Memory[MemoryModule<br/>deps: none]
    Bus --- MCP[MCPModule<br/>deps: tools]
    Bus --- Store[StoreModule<br/>deps: none]
    Bus --- Cron[CronModule<br/>deps: none]
    Bus --- TUI[TuiModule<br/>TUI runs last]

    subgraph Kernel["alex/kernel/"]
        Contracts[contracts/]
        DTO[dto/]
        Proto[bus.py / runtime.py]
        Impl[host.py / errors.py]
    end
```

### 启动流程

```
1. entry.py:main()
   ├─ AsyncEventBus 创建
   ├─ ModuleHost 创建
   ├─ 通过 importlib.metadata.entry_points(group="alex.modules")
   │  发现所有模块类，register() 到 host
   ├─ host.start_all()
   │   ├─ bus.start()
   │   ├─ 拓扑排序 (memory → skill → tools → mcp → agent → store → cron)
   │   └─ 每个 module.start(bus): subscribe events / provide handlers
   └─ AlexApp(bus).run_async()
       └─ TUI 启动，用户输入 → bus.publish(UserTurnRequested)
          → AgentModule._on_user_turn() → chat_stream()
```

### 模块依赖关系

| 模块 | 依赖 | 职责 |
|------|------|------|
| `MemoryModule` | — | 提供 GetContext / AppendMessages / ClearMemory / ReplaceMemory |
| `SkillModule` | tools | 提供 RetrieveSkills / LoadSkill / ReflectSkills，注册 load_skill 工具 |
| `ToolsModule` | — | 提供 ExecuteTool / GetToolCatalog，收编 MCP + plugin 工具 |
| `MCPModule` | tools | 后台连接 MCP servers，通过 ToolsProvided 广播工具 |
| `AgentModule` | memory, tools, skill | 订阅 UserTurnRequested，驱动对话循环 |
| `CronModule` | — | 提供 ScheduleCron / CancelCron，发布 CronTurnRequested |
| `StoreModule` | — | 订阅 TurnCompleted 持久化 session，提供 ListSessions / LoadSession |
| `TuiModule` | — | 最后启动；订阅 UI 事件渲染，发布 UserTurnRequested |

---

## 模块文档索引

| 模块 | 文档 | 摘要 |
|------|------|------|
| Kernel 共享层 | *(本文档)* | 跨模块契约、DTO、MessageBus 协议、模块运行时 |
| Agent 对话引擎 | [agent.md](./agent.md) | AgentModule — 订阅 UserTurnRequested，通过 bus 与各模块交互 |
| TUI 交互界面 | [display.md](./display.md) | Textual TUI 应用，支持滚动、折叠、反馈、Markdown 渲染、权限确认 modal |
| 工具生态 | [tools.md](./tools.md) | ToolsModule 网关 + Registry + 权限策略 + 审计 + 用户插件 + MCP + 各内置工具 |
| LLM 客户端 | [llm.md](./llm.md) | 基于 OpenAI SDK 的统一 ChatClient，streaming + JSON-mode |
| 记忆管理 | [memory.md](./memory.md) | MemoryModule，BufferMemory 滑动窗口 |
| 流式输出 | [streaming.md](./streaming.md) | 基于 bus 事件的流式分发 |
| 自适应技能系统 | [skills.md](./skills.md) | SkillModule — 技能提炼/检索/进化 |
| 类型化事件系统 | [events.md](./events.md) | Kernel contracts — Event / Command / Request 三层语义 |
| Bus 事件速查 | [bus.md](./bus.md) | 各模块 bus 事件速查——订阅 / 发布 / 提供的完整索引 |
| 配置管理 | [config.md](./config.md) | 多 provider 配置加载，支持文件和环境变量 |

---

## 项目结构

```
alex/
├── kernel/                      # 共享 kernel — 零业务逻辑
│   ├── bus.py                   # MessageBus Protocol (Event / Command / Request)
│   ├── runtime.py               # Module / ModuleHost Protocol
│   ├── host.py                  # 具体 ModuleHost 实现（拓扑排序启动）
│   ├── errors.py                # CapabilityTimeout / CapabilityUnavailable / HandlerError
│   ├── contracts/               # 所有跨模块消息类型
│   │   ├── __init__.py          #   统一导出
│   │   ├── chat.py              #   UserTurnRequested, TurnCompleted, TokenEmitted, ThinkingUpdated...
│   │   ├── tools.py             #   ExecuteTool, GetToolCatalog, ToolStarted, ToolApprovalRequested...
│   │   ├── skills.py            #   RetrieveSkills, LoadSkill, ReflectSkills, SkillsReflected...
│   │   ├── memory.py            #   GetContext, AppendMessages, ClearMemory, ReplaceMemory
│   │   ├── session.py           #   ListSessions, LoadSession, SessionRestored
│   │   └── cron.py              #   ScheduleCron, CancelCron, CronTurnRequested, CronJobEvent
│   └── dto/                     # 共享 DTO（纯 dataclass）
│       ├── __init__.py
│       ├── message.py           #   MessageDTO
│       ├── skill.py             #   SkillCard
│       ├── tool.py              #   ToolSpec, ToolResult, ToolExecutionContext
│       └── approval.py          #   ToolApprovalRequest (preview / summary)
├── agent/                       # Agent 模块
│   ├── module.py                #   AgentModule — 订阅 UserTurnRequested，实现 TurnServices
│   ├── service.py               #   Agent — 薄 facade，仅依赖 bus
│   ├── chat_service.py          #   ChatAppService — LLM 循环 + 工具执行
│   └── turn_processor.py        #   TurnProcessor — 统一 user/cron turn FIFO
├── bus/                         # 事件总线实现
│   ├── events.py                #   遗留事件类型（CronJobEvent, CronDebugEvent）
│   └── in_memory.py             #   AsyncEventBus — 具体 MessageBus 实现
├── memory/                      # 记忆模块
│   ├── module.py                #   MemoryModule (provides GetContext / AppendMessages)
│   ├── base.py                  #   MemoryBase ABC
│   ├── buffer.py                #   BufferMemory 滑动窗口
│   └── factory.py               #   MemoryModule 工厂
├── skill/                       # 技能模块
│   ├── module.py                #   SkillModule (provides RetrieveSkills / LoadSkill)
│   ├── service.py               #   SkillService — 业务逻辑
│   ├── repository.py            #   SkillStore JSON
│   ├── matcher.py               #   SkillRetriever
│   ├── reflector.py             #   LLM 反思
│   ├── evolution.py             #   生命周期
│   ├── models.py                #   Skill 数据类
│   ├── ports.py                 #   SkillServicePort Protocol
│   └── factory.py               #   SkillModule 工厂
├── store/                       # 持久化模块
│   ├── module.py                #   StoreModule (subscribes TurnCompleted)
│   ├── session.py               #   文件 I/O
│   ├── session_serializer.py    #   BaseMessage <-> dict
│   └── session_adapter.py       #   SessionPersistence 事件驱动
├── scheduler/                   # Cron 调度模块（三向拆分）
│   ├── module.py                #   CronModule (provides ScheduleCron / CancelCron / ListCronJobs)
│   ├── manager.py               #   CronManager — APScheduler + job 生命周期
│   ├── cron_executor.py         #   CronExecutor — runner 标准化 + execute-once
│   └── cron_store.py            #   CronStore — durable job 原子持久化
├── tools/                       # 工具网关模块
│   ├── module.py                #   ToolsModule (provides ExecuteTool / GetToolCatalog)
│   ├── registry.py              #   ToolRegistry
│   ├── executor.py              #   ToolExecutor + 权限检查
│   ├── models.py                #   AlexTool — 自定义工具类（替代 LangChain StructuredTool）
│   ├── permissions.py           #   PermissionPolicy + AuditLogger + summariser
│   ├── plugin_loader.py         #   用户插件
│   ├── mcp_client.py            #   (遗留) MCP 客户端已在 alex/mcp/ 独立
│   ├── _path.py / _binary.py    #   共享路径校验 + 二进制检测
│   ├── fs.py                    #   read / write / edit + FileReadTracker
│   ├── search.py                #   grep / glob
│   ├── shell.py                 #   bash / pwsh
│   ├── git.py                   #   git_inspect
│   ├── time.py / web_search.py / web_fetch.py / cron.py
├── mcp/                         # MCP 模块（独立于 tools）
│   ├── module.py                #   MCPModule — 后台连接 + ToolsProvided 广播
│   └── mcp_client.py            #   MCP 多 transport 客户端 + tool 适配
├── tui/                         # TUI 模块
│   ├── module.py                #   TuiModule — 发布 UserTurnRequested，路由事件到 app
│   ├── app.py / alex.tcss       #   AlexApp 主类 + CSS 样式表
│   ├── ports.py                 #   _ControllerHost Protocol
│   ├── controller.py            #   命令分发、session、toggles
│   ├── chat_projector.py        #   bus → widget 投影
│   ├── notification_controller.py # toast / feedback / 权限确认
│   ├── confirm_screen.py        #   PermissionConfirmScreen modal
│   ├── view_state.py / view_models.py / cron_history.py
│   ├── presenter.py             #   AlexBubble / UserBubble / ToolBubble
│   ├── stream_renderer.py       #   共享流式渲染状态管理
│   ├── tool_display.py          #   工具输出渲染组件
│   └── markdown.py              #   render_response — Rich Markdown 渲染层
├── llm/                         # LLM 客户端
│   ├── factory.py               #   LLMFactory
│   ├── base.py                  #   LLMConfig
│   └── client.py                #   ChatClient (OpenAI SDK, streaming + JSON-mode)
├── prompts/                     # Jinja2 提示词模板
├── entry.py                     # 生产入口 — wires modules via ModuleHost
├── app_logging.py               # 日志配置（RotatingFileHandler）
├── messages.py                  # 纯 dict 消息类型（替代 langchain_core.messages）
└── config.py                    # 环境变量配置 + MCP 配置解析
```

---

## 后续演进方向

1. **技能向量化检索** — `SkillRetriever` 升级为 embedding 语义匹配
2. **Cron 任务变化检测** — `last_result_hash` 减少噪音
3. **可观测性** — Token 成本看板、OpenTelemetry trace、replay 模式
4. **Web API** — FastAPI + SSE/WebSocket 暴露流式接口
5. **多入口** — VS Code 扩展、headless 模式、`alex serve` daemon
6. **跨用户技能共享** — 多用户场景下的技能市场/共享池
7. **TUI 增强** — 多模态输入、会话搜索、消息编辑/重生成
8. **稳定性** — `SkillStore` / `SessionRepository` 原子写、port contract tests
