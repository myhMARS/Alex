# Alex 模块化重构设计文档

## 文档定位

本文档描述两件事：

1. 我认为 Alex 的理想技术架构应该长什么样
2. 当前实现距离该目标还差哪些边界、抽象、状态模型和执行链路

这不是“当前代码已经重构完成”的验收稿，而是一份面向未来多个迭代的架构蓝图。它优先反映运行时真实结构，其次再定义理想目标和演进路径。

---

## 结论摘要

Alex 已经从早期的”`Agent + TUI + 工具集合`”单体，演进成模块化单体 v2.4：

- 目录结构已经按模块拆分
- typed event 已统一，event bus 角色明确（event-only）
- session、cron、stream 渲染等关键链路已经稳定
- Application Layer 已拆分为 5 个独立 service（Phase 2，2026-05-26）
- Port/adapter 已全对齐：`SessionRepository`、`SkillServicePort`、`AgentFacade`
- TUI 已拆分为 ChatProjector / SessionViewState / NotificationController（Phase 3，2026-05-27）
- controller.py 从 608 行降至 282 行（-54%）
- `ToolExecutionContext` is first-class runtime context（Phase 1/4，2026-05-28）
- `SessionSerializer` 消除 SessionService 对 store 内部的 import（Phase 4，2026-05-28）
- `CronHistoryReadModel` 从 ChatHistory 独立（Phase 4，2026-05-28）
- `create_agent()` factory 替代手工 wiring（Phase 4，2026-05-28）
- `push_notification` 语义收口，单一发布路径（Phase 4，2026-05-28）

当前剩余差距集中在：

1. composition helper 仍未收口到单一模块（`factory.py` / `service.py` 仍有重复构造逻辑）
2. Cron 虽然稳定可用，但工具入口、生命周期恢复、触发执行的 ownership 仍分散
3. Read model 仍主要靠 `ChatHistory` 与即时渲染状态，尚未按“真实共享派生状态”增量拆分

因此，当前最准确的判断是：

- 当前架构：模块化单体 v2.4（主路径稳定，边界已基本成型）
- 下一阶段：在不引入过度抽象的前提下，继续收口 wiring、校正 cron ownership、按需抽取 read model

---

## 必须保留的约束

后续所有重构必须保持以下行为不变：

1. 使用 DeepSeek thinking mode 时，所有 `AIMessage` 都必须保留 `reasoning_content`
2. `/help`、`/skills` 等 TUI modal 视图必须拦截文本输入，并通过 `:q` 退出
3. 所有后台任务必须绑定到 Textual 主事件循环，或显式转移到线程池/后台 worker
4. Session 持久化必须保存原始 `BaseMessage` 序列，而不是 UI 视图模型
5. cron 的 durable 任务定义允许落盘到 `~/.alex/cron/` 并在重启后恢复到当前会话；关闭 Alex 时不后台执行，执行历史仍按 session 保存
6. `/resume` 恢复路径必须与实时流式渲染路径表现一致
7. 同一 session 的 turn 必须保持顺序一致性

---

## 理想架构

### 总体目标

理想状态不是微服务，而是“强边界的进程内模块化单体”。它应该具备以下特征：

1. `TUI` 只负责输入、投影和交互状态，不负责业务编排
2. application services 负责命令处理和流程调度，不直接做文件 I/O
3. domain services 负责业务规则，不感知 TUI、Textual、文件系统
4. adapters 负责与文件、LLM、APScheduler、外部 Web API 通信
5. event bus 只承担事件传播；command handling 要么显式走 command bus，要么明确保持 direct-call 模式，不能混合叙事
6. 所有 session 级可变状态都必须可定位、可重置、可测试

### 理想分层

```text
Presentation Layer
  TUI App / Controller / Projector / ViewState

Application Layer
  ChatAppService
  SessionAppService
  CronAppService
  FeedbackAppService
  SkillAdminAppService

Domain Layer
  TurnEngine
  PromptPolicy
  ReflectionPolicy
  CronExecutionPolicy
  ToolExecutionContext
  SessionStateModel

Adapter Layer
  EventBusAdapter
  SessionRepository
  SkillRepository
  LLMGateway
  SchedulerAdapter
  ToolRegistryAdapter
  WebSearchAdapter / WebFetchAdapter

Projection / Read Model Layer
  ChatTimelineProjector
  CronHistoryProjector
  SessionListProjector
  SkillListProjector
```

### 理想运行关系

```text
TUI command
  -> Application Service
      -> Domain Services
      -> Repositories / Gateways / Runtime adapters
      -> publish Domain Events
          -> Projectors update read models / UI state
```

### 理想架构原则

1. Command 与 Event 语义分离
2. Read model 与 write model 分离
3. session 级状态集中管理
4. adapter 可替换，port 不漂移
5. 所有异步行为都要么显式非阻塞，要么显式转线程
6. 文档中的并发语义必须与实现完全一致

---

## 当前实现快照

### 当前目录结构

```text
alex/
├── agent/
│   ├── factory.py              # create_agent() — explicit wiring function
│   ├── service.py              # Agent — thin facade, wiring only
│   ├── chat_service.py         # ChatAppService — chat_stream, tool exec, graph
│   ├── session_service.py      # SessionService — session persistence boundary
│   ├── cron_service.py         # CronService — cron schedule/cancel/lifecycle
│   ├── feedback_service.py     # FeedbackAppService — rating, episode, reflection
│   ├── skill_admin_service.py  # SkillAdminAppService — skill CRUD, merge
│   ├── turn_processor.py       # TurnProcessor — unified user/cron FIFO execution
│   ├── prompt.py               # PromptAssembler — system prompt + skills section
│   └── ports.py                # AgentFacade Protocol
├── bus/
│   ├── events.py               # Event → Command/DomainEvent/UIEvent hierarchy
│   └── in_memory.py            # AsyncEventBus — subscriber-based event dispatch
├── memory/
│   ├── base.py                 # MemoryBase ABC
│   ├── buffer.py               # BufferMemory — sliding window
│   └── ports.py                # MemoryService Protocol
├── skill/
│   ├── models.py               # Skill dataclass
│   ├── service.py              # SkillService — constructor injection
│   ├── repository.py           # SkillStore — JSON persistence
│   ├── matcher.py              # SkillRetriever — pattern/keyword matching
│   ├── reflector.py            # Reflector — LLM-based skill reflection
│   ├── evolution.py            # EvolutionEngine — lifecycle transitions
│   └── ports.py                # SkillServicePort Protocol (aligned)
├── store/
│   ├── session.py              # Session file I/O + serialize/deserialize
│   ├── session_serializer.py   # BaseMessage ↔ dict roundtrip (agent-layer safe)
│   ├── session_adapter.py      # SessionPersistence — event-driven auto-save
│   └── ports.py                # SessionRepository Protocol (aligned)
├── tools/
│   ├── cron.py                 # cron tool interface
│   ├── executor.py             # ToolExecutor
│   ├── registry.py             # ToolRegistry
│   ├── permissions.py          # PermissionPolicy + AuditLogger + approval summariser
│   ├── plugin_loader.py        # user plugin discovery + loading
│   ├── mcp_client.py           # MCP stdio client + tool adapter
│   ├── fs.py                   # read / write / edit + FileReadTracker
│   ├── search.py               # grep / glob
│   ├── shell.py                # bash / pwsh
│   ├── git.py                  # git_inspect
│   ├── time.py                 # Time tool
│   ├── web_fetch.py            # Web fetch tool
│   ├── web_search.py           # Web search tool
│   └── ports.py                # ToolExecutionContext / CronScheduler Protocols
├── scheduler/
│   └── manager.py              # CronManager — APScheduler wrapper
├── tui/
│   ├── app.py                  # AlexApp — Textual App, wiring center
│   ├── controller.py           # ChatControllerMixin — commands, session, toggles
│   ├── chat_projector.py       # ChatProjector — bus→widget projection, cron renderers
│   ├── notification_controller.py # NotificationController — toast, feedback
│   ├── view_state.py           # SessionViewState — UI-only mutable state dataclass
│   ├── presenter.py            # Bubble components (AlexBubble, etc.)
│   ├── view_models.py          # ChatHistory, ChatTurn
│   ├── cron_history.py         # CronHistoryReadModel — standalone read model
│   ├── confirm_screen.py       # PermissionConfirmScreen — permission confirmation modal
│   ├── markdown.py             # render_response — Rich Markdown rendering
│   └── stream_renderer.py      # StreamRenderer — shared user/cron rendering
└── prompts/
```

### 当前真实关系

```text
TUI
  AlexApp (wiring center)
    ├── ChatControllerMixin  (commands, session lifecycle, toggles)
    ├── ChatProjector        (bus→widget projection, cron renderers, status bar)
    ├── NotificationController (toast, feedback prompt, rating)
    ├── SessionViewState     (UI-only mutable state, reset on session switch)
    └── AgentFacade (thin facade)
          -> ChatAppService     (chat_stream, tool exec, graph, cron history)
          -> SessionService     (session persistence boundary)
          -> CronService        (cron schedule/cancel/lifecycle)
          -> FeedbackAppService (feedback recording, per-session state, reflection)
          -> SkillAdminAppService (skill CRUD, merge, load_skill)
          -> EventBus
              -> SessionPersistence (auto-save on TurnCompleted / CronJobEvent)
              -> ChatProjector (cron/skill event → widget updates)
```

### 当前架构已完成的里程碑（压缩版）

以下内容已经可以视为“现状基线”，不再在本文档中逐条展开：

| 阶段 | 已完成内容 |
|------|------------|
| Phase 1 | port 语义对齐：`SessionRepository`、`SkillServicePort`、`AgentFacade`；feedback 改为 per-session state |
| Phase 2 | Application Layer 拆分完成：`ChatAppService`、`FeedbackAppService`、`SkillAdminAppService`；`Agent` 降为 facade |
| Phase 3 | TUI 薄化完成：`ChatProjector`、`SessionViewState`、`NotificationController` 分离；`controller.py` 明显收缩 |
| Phase 4 | runtime 边界收口：`ToolExecutionContext` 一等化、`SessionSerializer` 抽离、`CronHistoryReadModel` 独立、factory wiring 建立 |
| Phase 5 | adapter 与测试治理增强：`SkillManager` 移除、`SkillStore` 原子写、contract/state/event 语义测试补齐 |

保留这个摘要的目的，是让后文聚焦“还没解决的结构问题”，而不是重复记录已经完成的重构履历。

### 当前架构仍存在的核心问题

1. 组合根仍有重复：`_create_default_skill_service` 在 `factory.py` 和 `service.py` 中重复定义，wiring 规则没有收口到单一模块
2. Cron 的职责边界仍不够清晰：工具入口、调度恢复、触发执行、事件投影虽然都已工作，但 ownership 仍分散在 `tools/cron.py`、`CronService`、`CronManager` 与统一 turn 执行路径之间
3. Read model 仍以 `ChatHistory` 为中心；虽然 cron history 已独立，但 session list、feedback 等读状态尚未形成稳定抽象

---

## 当前架构与理想架构的差距

### 差距总表

| 领域 | 当前架构 | 理想架构 | 差距等级 |
|------|----------|----------|----------|
| Agent / Application | ✅ 薄 facade 组合 5 个 app service，wiring 在 Agent.__init__ | 单一 composition root，默认依赖创建规则不重复 | 低 |
| Session / Store | ✅ `SessionService` 通过 `session_serializer` 访问，不再直接 import store 内部 | - | 已解决 |
| Skill | ✅ `SkillServicePort` 已对齐 `SkillService`，`SkillManager` 已移除 | `SkillStore` 支持向量化检索 | 低 |
| Event System | ✅ typed event + event-only bus 语义明确 | 维持 direct-call + event-only bus，不再引入额外 command bus 叙事 | 低 |
| TUI / Projection | ✅ controller 已薄化至 282 行，ChatProjector / SessionViewState / NotificationController 已分离 | - | 已解决 |
| Tool Runtime | ✅ `ToolExecutionContext` 为一等对象，executor 和所有 caller 已对齐 | - | 已解决 |
| Cron / Scheduler | 已稳定可用，但工具入口、生命周期恢复、触发执行的职责仍分散 | 明确 ownership：工具负责参数面，`CronService` 负责应用级生命周期，`CronManager` 保持基础设施 adapter，`TurnProcessor` 负责统一执行 | 中 |
| Feedback / Reflection | ✅ `FeedbackAppService` + `FeedbackSessionState` per-session 字典 | episodes 持久化为独立日志（可选） | 低 |
| Read Models | 主要靠 `ChatHistory` 与即时渲染状态 | 明确 projector + read model 边界 | 中 |
| Tests / Governance | ✅ contract / state / event 语义测试已补齐 | 持续让关键架构约束有对应测试映射 | 低 |

---

## 分模块详细设计路线

下面每一节都按四个维度描述：

1. 当前问题
2. 目标设计
3. 重构路线
4. 验收标准

### 1. 已完成模块（压缩版）

以下模块已经达到“当前架构可接受”的状态，不再作为后续重构重点：

- Agent / Application Layer：`Agent` 已是薄 facade，主要业务入口在 application services
- Session / Store Boundary：`SessionRepository` / `SessionSerializer` / `SessionService` 语义已经对齐，agent 不再直接穿透 store 实现
- Skill 模块：`SkillService` 为统一入口，`SkillAdminAppService` 承接管理操作
- Event System：已明确采用 direct-call application service + event-only bus
- TUI / Projection：controller、projector、notification、view state 已分离
- Tool Runtime：`ToolExecutionContext` 已成为稳定 runtime contract
- Feedback / Reflection：状态已按 session 隔离
- Testing / Governance：核心 port / state / event 语义已进入测试保护

后续只需在这些模块上做增量维护，不再建议为了“架构纯度”继续大拆。

### 2. Cron / Scheduler

#### 当前问题

cron 功能已经稳定（`cron` 工具调度 + `cron_jobs` 查询 + APScheduler + cron reply 链路），但 lifecycle 与 ownership 仍分散在三处：

- `CronManager`（scheduler/manager.py）— APScheduler 封装 + job 生命周期
- `TurnProcessor`（agent/turn_processor.py）— user/cron 共用的 FIFO 执行与流式事件分发
- `Agent._cron`（agent/service.py）— `CronService` 薄包装

cron 本质上是一条“工具触发 + 调度恢复 + 异步执行 + 事件投影”的跨层链路。当前问题不是 `CronService` 存在本身，而是这条链路的职责边界还没有被文档清楚说透。

#### 目标设计（调整后）

```
tools/cron.py               # cron 工具的 LangChain 接口（schedule/cancel/list）
    │
    ▼
agent/turn_processor.py     # user/cron 共用的 FIFO 执行器
    │
    ▼
scheduler/manager.py        # CronManager — 对 APScheduler 的纯适配（基础设施）
    │
    ▼
bus/events.py               # CronJobEvent — 异步结果通过 EventBus 发布
    │
    ├── store/session_adapter.py  # 持久化（事件驱动，不参与执行决策）
    └── tui/chat_projector.py    # TUI 渲染（事件驱动，不参与业务决策）
```

基于当前实现，我认为原先“必须移除 `CronService`”的目标过度追求纯度，收益有限。更合理的目标是：

1. 工具层继续负责参数面与 LLM 可见接口
2. `CronService` 保留为很薄的应用级生命周期边界，负责启动恢复、session 绑定、对 Agent 的稳定 API
3. `CronManager` 继续作为 APScheduler adapter，不向上暴露过多执行细节
4. `TurnProcessor` 统一承接 user/cron 执行，不再保留额外的 cron 专用执行器

核心原则从“删掉 `CronService`”调整为“**明确 ownership，避免重复入口**”。

#### 重构路线

Phase A:

1. 收口 `CronService` 的职责说明，使其只保留 lifecycle / restore / facade API，不新增业务逻辑
2. `CronManager` 继续暴露 schedule / cancel / list_jobs，由 `CronService` 统一承接
3. 清理文档与代码中的旧叙事，避免同时存在“cron 是独立 app service”和“cron 完全不是 app service”两套说法

Phase B:

1. 保持 `TurnProcessor` 为唯一 turn 执行入口，不再引入新的 coordinator 抽象
2. 检查启动恢复、durable 任务重绑当前 session、状态栏刷新等语义是否都由单一路径保障

Phase C:

1. `tools/cron.py` 保持调度入口，继续与其他工具走一致的注册路径
2. 如后续确实出现多宿主（TUI / headless / daemon）场景，再重新评估是否让 `CronService` 下沉或消失

#### 验收标准

1. `CronService` 的存在理由和边界在文档与实现中一致
2. cron 工具注册路径与其他工具一致（`ToolRegistry.register`）
3. TUI 只消费 `CronJobEvent` 投影，store 只消费持久化事件

### 3. Read Models / Projection

#### 当前问题

当前 `ChatHistory` 已经很干净，但它仍然同时是：

- TUI 的主要 read model
- renderer finalize 的落点
- cron history 的内存缓存

随着 UI 能力增加，它仍然会继续变大。

#### 目标设计（调整后）

原计划一次性拆出 `ChatTimeline` / `CronHistoryReadModel` / `SessionListReadModel` / `FeedbackReadModel` 四套读模型，但按当前代码体量看，没必要一步到位。

更合理的顺序是：

1. 保留 `ChatHistory` 作为 timeline 容器
2. 继续让 `CronHistoryReadModel` 独立存在
3. 只在 `session list` 或 `feedback` 出现第二个消费者、第二套投影逻辑时，再抽 `SessionListReadModel` / `FeedbackReadModel`

判断标准不是“概念上是否纯”，而是“是否已经出现多处读路径共享同一份派生状态”。

#### 重构路线

Phase A:

1. 维持 `ChatHistory` 作为组合容器，而不是继续削成过细的对象网
2. 只对已经形成稳定读语义的状态抽独立 read model

Phase B:

1. 如果后续出现第二套 timeline 消费路径，再考虑让 `StreamRenderer.finalize()` 输出标准 `RenderedTurn`
2. 在那之前，优先保持 projector 写入路径简单直接

#### 验收标准

1. read model 与 renderer 的耦合度可控
2. 只有真正共享的派生状态才被抽成 read model

### 4. 组合根 / DI

#### 当前问题

目前最真实的技术债不是“没有 IoC 容器”，而是“组合逻辑没有唯一归属”：

- `factory.py` 和 `service.py` 各自维护 `_create_default_skill_service`
- wiring 规则存在重复，但复杂度还没高到必须引入外部 DI 框架

#### 目标设计（调整后）

短期目标应是 **收口 composition helper**，而不是立即引入 `punq` / `dependency-injector`：

1. 把默认 skill service、默认 permissions、默认 memory/llm 这些构造逻辑收敛到单一 composition 模块
2. `create_agent()` 继续作为主要 composition root
3. 只有在出现多宿主、多 profile、复杂测试装配矩阵时，再评估外部 DI container

#### 重构路线

Phase A:

1. 提取共享 builder，消除 `factory.py` / `service.py` 的重复构造逻辑
2. 保证默认依赖的创建规则只有一个源头

Phase B:

1. 若后续出现多个宿主（例如 headless runner / daemon / RPC host），再评估是否需要真正的 container
2. 评估标准以“是否减少复杂度”为准，而不是“是否更学院派”

#### 验收标准

1. 默认 wiring 规则只有一个定义位置
2. `Agent` 不再私自构造与 factory 重复的依赖
3. 在没有多宿主需求前，不引入额外 DI 框架

---

## 分阶段路线图

### 已完成阶段（归档摘要）

- Phase 1-5 已完成，覆盖 port 对齐、application/service 拆分、TUI 薄化、runtime 收口、adapter 强化与测试治理
- 这些阶段的细节不再作为主文档主体；如需追溯变更履历，应查看对应阶段报告

### Phase 6：Wiring 收口 + Cron ownership 校正 + 增量 Read Model (下一阶段)

目标：

- 收口 wiring 与 composition helper，消除重复构造逻辑
- 校正 cron ownership，使文档与实现对 `CronService` / `CronManager` / `TurnProcessor` 的分工保持一致
- 只在确有必要时增量抽取新的 read model

工作项：

1. 提取共享 composition helper，移除 `factory.py` / `service.py` 中重复的默认依赖创建逻辑
2. 保持 `CronService` 为薄 facade，但明确其只负责 lifecycle / restore / facade API，不再承载额外业务
3. 复核 cron 启动恢复、durable 任务重绑当前 session、状态栏刷新、`cron_jobs` 查询这些语义是否都由单一路径保障
4. 如 `session list` 或 feedback 出现第二套消费路径，再抽 `SessionListReadModel` / `FeedbackReadModel`
5. 清理文档中的历史叙事，避免继续保留“cron 必须完全回到工具层”的旧目标
6. 暂不引入外部 DI container；只有出现明显的多宿主/多 profile 需求时再评估

完成标志：

- 默认 wiring 规则只有一个定义位置
- cron 的 ownership 在文档、实现、测试中说法一致
- 只有真正共享的派生状态才会被抽成 read model

---

## 最终验收标准

当以下条件都满足时，才能称为“架构重构真正完成”：

1. `AgentFacade` 只负责组合与代理，不再承担主业务逻辑
2. `SessionRepository`、`SkillServicePort`、`ToolExecutionContext` 均为真实稳定边界
3. event bus 的语义在文档、实现、测试三处完全一致
4. ✅ TUI 中不再存在横跨命令解析、状态管理、事件投影、通知控制的超级 controller
5. 所有 session 级状态都有显式 owner
6. 所有关键架构约束都有 contract test 或语义测试保护

---

## 一句话总结

当前 Alex 已经是模块化单体 v2.4：主路径的 application service、event bus、TUI projector、tool runtime、session/store 边界和测试语义都已基本稳定。真正剩余的问题，不再是“大规模拆层”，而是把少数仍然模糊的 ownership 收口清楚。

下一阶段的重点不应再是“为了纯度继续拆”，而是：
1. 收口 wiring，消除重复构造逻辑
2. 校正 cron 的 ownership 叙事，使 `CronService` / `CronManager` / `TurnProcessor` 的边界稳定
3. 只在 UI 读状态出现多个消费者后，再增量抽 read model
4. 在出现真实需求前，不急于引入外部 DI container
