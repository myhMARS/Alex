# Alex 模块化重构设计文档

## 文档定位

本文档描述两件事：

1. 我认为 Alex 的理想技术架构应该长什么样
2. 当前实现距离该目标还差哪些边界、抽象、状态模型和执行链路

这不是"当前代码已经重构完成"的验收稿，而是一份面向未来多个迭代的架构蓝图。它优先反映运行时真实结构，其次再定义理想目标和演进路径。

---

## 结论摘要

Alex 已经从早期的"`Agent + TUI + 工具集合`"单体，演进成模块化单体 v2.6：

- 目录结构已经按模块拆分
- typed event 已统一，event bus 角色明确（event-only）
- session、cron、stream 渲染等关键链路已经稳定
- Application Layer 已拆分为 5 个独立 service（Phase 2，2026-05-26）
- Port/adapter 已全对齐：`SessionRepository`、`SkillServicePort`、`AgentFacade`
- TUI 已拆分为 ChatProjector / SessionViewState / NotificationController（Phase 3，2026-05-27）
- controller.py 从 608 行降至 282 行（-54%）
- `ToolExecutionContext` is first-class runtime context（Phase 4，2026-05-28）
- `CronHistoryReadModel` 从 ChatHistory 独立（Phase 4，2026-05-28）
- `create_agent()` factory 替代手工 wiring（Phase 4，2026-05-28）
- `push_notification` 语义收口，单一发布路径（Phase 4，2026-05-28）
- 共享 `composition.py` 收口默认构造逻辑，`factory.py` 和 `service.py` 均从此导入（Phase 5，2026-05-29）
- 共享 `_path.py` / `_binary.py` 消除 tools 重复代码（Phase 5，2026-05-29）
- `LLMConfig` 显式注入链替代 `llm` 假注入（Phase 5，2026-05-29）
- `json_client.py` client 缓存，按 config digest 复用连接池（Phase 5，2026-05-29）
- CSS 外置到 `alex.tcss`，`_ProjectorHost` Protocol 约束 TUI 类型安全（Phase 5，2026-05-29）
- `CronManager` cross-thread 辅助提取 + `NormalizedCronRunner` 定义（Phase 5，2026-05-29）

当前剩余差距集中在：

1. `CronManager` 仍承担多职责（~558 行），可进一步拆分为 scheduler / executor / store，但当前 ownership 链路（Agent → CronService → CronManager → APScheduler）已清晰
2. Read model 仍主要靠 `ChatHistory` 与即时渲染状态，尚未按"真实共享派生状态"增量拆分
3. `ChatControllerMixin` duck typing 可进一步用 Protocol 约束

因此，当前最准确的判断是：

- 当前架构：模块化单体 v2.6（主路径稳定，依赖注入链路完整，wiring 已收口到 composition.py）
- 下一阶段：CronManager 职责细分（可选）、Read model 增量抽取、TUI controller type safety

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

理想状态不是微服务，而是"强边界的进程内模块化单体"。它应该具备以下特征：

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
│   ├── composition.py           # 共享默认依赖构造（单一来源）
│   ├── factory.py               # create_agent() — 显式 wiring 入口
│   ├── service.py               # Agent — thin facade，从 composition 导入
│   ├── chat_service.py          # ChatAppService — chat_stream, tool exec, graph
│   ├── session_service.py       # SessionService — session persistence boundary
│   ├── cron_service.py          # CronService — 薄包装，委托 CronManager（57 行）
│   ├── feedback_service.py      # FeedbackAppService — rating, episode, reflection
│   ├── skill_admin_service.py   # SkillAdminAppService — skill CRUD, merge
│   ├── turn_processor.py        # TurnProcessor — unified user/cron FIFO execution
│   ├── prompt.py                # PromptAssembler — system prompt + skills section
│   └── ports.py                 # AgentFacade Protocol
├── bus/
│   ├── events.py                # Event → Command/DomainEvent/UIEvent hierarchy
│   └── in_memory.py             # AsyncEventBus — subscriber-based event dispatch
├── memory/
│   ├── base.py                  # MemoryBase ABC
│   ├── buffer.py                # BufferMemory — sliding window
│   └── ports.py                 # MemoryService Protocol
├── skill/
│   ├── models.py                # Skill dataclass
│   ├── service.py               # SkillService — constructor injection
│   ├── repository.py            # SkillStore — JSON persistence
│   ├── matcher.py               # SkillRetriever — pattern/keyword matching
│   ├── reflector.py             # Reflector — LLM-based skill reflection
│   ├── evolution.py             # EvolutionEngine — lifecycle transitions
│   └── ports.py                 # SkillServicePort Protocol (aligned)
├── store/
│   ├── session.py               # Session file I/O + serialize/deserialize
│   ├── session_serializer.py    # BaseMessage ↔ dict roundtrip (agent-layer safe)
│   ├── session_adapter.py       # SessionPersistence — event-driven auto-save
│   └── ports.py                 # SessionRepository Protocol (aligned)
├── tools/
│   ├── cron.py                  # cron tool interface
│   ├── executor.py              # ToolExecutor
│   ├── registry.py              # ToolRegistry
│   ├── permissions.py           # PermissionPolicy + AuditLogger + approval summariser
│   ├── plugin_loader.py         # user plugin discovery + loading
│   ├── mcp_client.py            # MCP stdio client + tool adapter
│   ├── fs.py                    # read / write / edit + FileReadTracker
│   ├── search.py                # grep / glob
│   ├── shell.py                 # bash / pwsh
│   ├── git.py                   # git_inspect
│   ├── time.py                  # Time tool
│   ├── web_fetch.py             # Web fetch tool
│   ├── web_search.py            # Web search tool
│   └── ports.py                 # ToolExecutionContext / CronScheduler Protocols
├── scheduler/
│   └── manager.py               # CronManager — APScheduler wrapper（~558 行）
├── tui/
│   ├── app.py                   # AlexApp — Textual App, wiring center
│   ├── controller.py            # ChatControllerMixin — commands, session, toggles
│   ├── chat_projector.py        # ChatProjector — bus→widget projection, cron renderers
│   ├── notification_controller.py # NotificationController — toast, feedback
│   ├── view_state.py            # SessionViewState — UI-only mutable state dataclass
│   ├── presenter.py             # Bubble components (AlexBubble, etc.)
│   ├── view_models.py           # ChatHistory, ChatTurn
│   ├── cron_history.py          # CronHistoryReadModel — standalone read model
│   ├── confirm_screen.py        # PermissionConfirmScreen — permission confirmation modal
│   ├── markdown.py              # render_response — Rich Markdown rendering
│   └── stream_renderer.py       # StreamRenderer — shared user/cron rendering
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
          -> CronService        (cron schedule/cancel/lifecycle, 57 行纯委托)
          -> FeedbackAppService (feedback recording, per-session state, reflection)
          -> SkillAdminAppService (skill CRUD, merge, load_skill)
          -> EventBus
              -> SessionPersistence (auto-save on TurnCompleted / CronJobEvent)
              -> ChatProjector (cron/skill event → widget updates)
```

### 当前架构已完成的里程碑（压缩版）

以下内容已经可以视为"现状基线"，不再在本文档中逐条展开：

| 阶段 | 已完成内容 |
|------|------------|
| Phase 1 | port 语义对齐：`SessionRepository`、`SkillServicePort`、`AgentFacade`；feedback 改为 per-session state |
| Phase 2 | Application Layer 拆分完成：`ChatAppService`、`FeedbackAppService`、`SkillAdminAppService`；`Agent` 降为 facade |
| Phase 3 | TUI 薄化完成：`ChatProjector`、`SessionViewState`、`NotificationController` 分离；`controller.py` 明显收缩 |
| Phase 4 | runtime 边界收口：`ToolExecutionContext` 一等化、`SessionSerializer` 抽离、`CronHistoryReadModel` 独立、factory wiring 建立 |
| Phase 5 | adapter 与测试治理增强：`SkillManager` 移除、`SkillStore` 原子写、contract/state/event 语义测试补齐；wiring 收口到 `composition.py`；CSS 外置；`CronManager` cross-thread 辅助提取 |

保留这个摘要的目的，是让后文聚焦"还没解决的结构问题"，而不是重复记录已经完成的重构履历。

### 当前架构仍存在的核心问题

1. `CronManager` 体量偏大（~558 行），混合了调度、执行、持久化三种职责。ownership 链路（Agent → CronService → CronManager → APScheduler）已清晰，进一步拆分属于优化而非补漏
2. Read model 仍以 `ChatHistory` 为中心；虽然 cron history 已独立，但 session list、feedback 等读状态尚未形成稳定抽象。当前没有第二个消费者，暂不急于拆分
3. `ChatControllerMixin` 通过 duck typing 与 `AlexApp` 交互，可进一步用 Protocol 约束类型安全

---

## 当前架构与理想架构的差距

### 差距总表

| 领域 | 当前架构 | 理想架构 | 差距等级 |
|------|----------|----------|----------|
| Agent / Application | ✅ 薄 facade 组合 5 个 app service，wiring 在 `composition.py` 单一收口 | 单一 composition root | 已解决 |
| Session / Store | ✅ `SessionService` 通过 `session_serializer` 访问，不再直接 import store 内部 | - | 已解决 |
| Skill | ✅ `SkillServicePort` 已对齐 `SkillService`，`SkillManager` 已移除 | `SkillStore` 支持向量化检索 | 低 |
| Event System | ✅ typed event + event-only bus 语义明确 | 维持 direct-call + event-only bus | 低 |
| TUI / Projection | ✅ controller 已薄化至 282 行，ChatProjector / SessionViewState / NotificationController 已分离 | - | 已解决 |
| Tool Runtime | ✅ `ToolExecutionContext` 为一等对象，executor 和所有 caller 已对齐 | - | 已解决 |
| Cron / Scheduler | ✅ 链路清晰：Agent → CronService(57行) → CronManager → APScheduler；TurnProcessor 统一执行 | CronManager 可按 scheduler/executor/store 进一步拆分（可选） | 低 |
| Feedback / Reflection | ✅ `FeedbackAppService` + `FeedbackSessionState` per-session 字典 | episodes 持久化为独立日志（可选） | 低 |
| Read Models | 主要靠 `ChatHistory` 与即时渲染状态 | 明确 projector + read model 边界，按需增量抽取 | 低 |
| TUI Controller Types | `ChatControllerMixin` 依赖 duck typing | Protocol 约束 `_ProjectorHost` 类型安全 | 低 |
| Tests / Governance | ✅ contract / state / event 语义测试已补齐 | 持续让关键架构约束有对应测试映射 | 低 |

---

## 分模块详细设计路线

下面每一节都按四个维度描述：

1. 当前问题
2. 目标设计
3. 重构路线
4. 验收标准

### 1. 已完成模块（压缩版）

以下模块已经达到"当前架构可接受"的状态，不再作为后续重构重点：

- Agent / Application Layer：`Agent` 已是薄 facade，主要业务入口在 application services；wiring 已通过 `composition.py` 单一收口
- Session / Store Boundary：`SessionRepository` / `SessionSerializer` / `SessionService` 语义已经对齐，agent 不再直接穿透 store 实现
- Skill 模块：`SkillService` 为统一入口，`SkillAdminAppService` 承接管理操作
- Event System：已明确采用 direct-call application service + event-only bus
- TUI / Projection：controller、projector、notification、view state 已分离
- Tool Runtime：`ToolExecutionContext` 已成为稳定 runtime contract
- Feedback / Reflection：状态已按 session 隔离
- Testing / Governance：核心 port / state / event 语义已进入测试保护
- Wiring / Composition：`composition.py` 为默认依赖构造的唯一来源，`factory.py` 和 `service.py` 均从此导入

后续只需在这些模块上做增量维护，不再建议为了"架构纯度"继续大拆。

### 2. Cron / Scheduler

#### 当前问题

cron 功能已经稳定，ownership 链路清晰：

```
tools/cron.py               # cron 工具的 LangChain 接口（schedule/cancel/list）
    │
    ▼
agent/turn_processor.py     # user/cron 共用的 FIFO 执行器
    │
    ▼
scheduler/manager.py        # CronManager — APScheduler 适配 + job 生命周期 + 持久化
    │
    ▼
bus/events.py               # CronJobEvent — 异步结果通过 EventBus 发布
    │
    ├── store/session_adapter.py  # 持久化（事件驱动）
    └── tui/chat_projector.py    # TUI 渲染（事件驱动）
```

`CronService`（57 行）是纯代理层，逐方法委托 `CronManager`。唯一可改进的是 `CronManager` 自身 ~558 行混合了调度、执行、持久化三种职责，可以按需进一步拆分。

#### 目标设计

1. 保持当前 ownership 链路不变
2. `CronManager` 可选拆分为 `CronScheduler` / `CronExecutor` / `CronStore`，但当前没有多实现需求，拆分收益有限
3. 出现多宿主（TUI / headless / daemon）场景时再评估

#### 重构路线

Phase A（当前状态 — 已稳定）:

1. `CronService` 保持薄 facade，只负责 lifecycle / restore / facade API
2. `CronManager` 继续暴露 schedule / cancel / list_jobs
3. `TurnProcessor` 为唯一 turn 执行入口

Phase B（可选，按需触发）:

1. 若 `CronManager` 持续增长或出现第二个 scheduler 实现，再拆 scheduler / executor / store
2. 若出现多宿主场景，再重新评估 `CronService` 的定位

#### 验收标准

1. cron 工具注册路径与其他工具一致（`ToolRegistry.register`）
2. TUI 只消费 `CronJobEvent` 投影，store 只消费持久化事件
3. 启动恢复、durable 任务重绑、状态栏刷新均由单一路径保障

### 3. Read Models / Projection

#### 当前问题

当前 `ChatHistory` 同时是：

- TUI 的主要 read model
- renderer finalize 的落点
- cron history 的内存缓存

随着 UI 能力增加，它仍然会继续变大。

#### 目标设计

1. 保留 `ChatHistory` 作为 timeline 容器
2. 继续让 `CronHistoryReadModel` 独立存在
3. 只在 `session list` 或 `feedback` 出现第二个消费者、第二套投影逻辑时，再抽 `SessionListReadModel` / `FeedbackReadModel`

判断标准不是"概念上是否纯"，而是"是否已经出现多处读路径共享同一份派生状态"。

#### 重构路线

Phase A（当前状态）:

1. 维持 `ChatHistory` 作为组合容器
2. 只对已经形成稳定读语义的状态（cron history）抽独立 read model

Phase B（按需触发）:

1. 如果后续出现第二套 timeline 消费路径，再考虑让 `StreamRenderer.finalize()` 输出标准 `RenderedTurn`
2. 在那之前，优先保持 projector 写入路径简单直接

#### 验收标准

1. read model 与 renderer 的耦合度可控
2. 只有真正共享的派生状态才被抽成 read model

### 4. 组合根 / DI

#### 当前状态

`composition.py` 已是默认依赖构造的**唯一来源**：

```python
# composition.py — 四个 factory 函数，单一定义点
create_default_config()       # LLMConfig
create_default_llm()          # BaseChatModel
create_default_memory()       # BufferMemory
create_default_skill_service() # SkillService
```

`factory.py` 和 `service.py` 都从 `composition` 导入，不存在重复定义。`create_agent()` 是主要 composition root。

#### 目标设计

1. 维持现状：`composition.py` 为单一来源，`create_agent()` 为 composition root
2. 只有在出现多宿主、多 profile、复杂测试装配矩阵时，再评估外部 DI container（如 `punq`）
3. 评估标准以"是否减少复杂度"为准，而不是"是否更学院派"

#### 验收标准

1. ✅ 默认 wiring 规则只有一个定义位置（`composition.py`）
2. ✅ `Agent` 不再私自构造与 factory 重复的依赖
3. ✅ 在没有多宿主需求前，不引入额外 DI 框架

### 5. TUI Controller Type Safety

#### 当前问题

`ChatControllerMixin` 通过 duck typing 访问 `AlexApp` 的属性（如 `projector`、`view_state`、`notification_controller`），没有显式类型约束。

#### 目标设计

用 Protocol 定义 `_ProjectorHost`，让 `ChatControllerMixin` 的方法签名显式声明依赖。

#### 重构路线

1. 在 `chat_projector.py` 或新建 `tui/ports.py` 中定义 `ProjectorHost` Protocol
2. `ChatControllerMixin` 的方法通过 Protocol 类型标注访问 host 属性
3. `AlexApp` 隐式满足 Protocol，无需显式继承

#### 验收标准

1. `ChatControllerMixin` 中对 `self.projector` / `self.view_state` 等属性的访问有类型检查
2. 不引入运行时开销（Protocol 是 structural subtyping）

---

## 分阶段路线图

### 已完成阶段（归档摘要）

- Phase 1-5 已完成，覆盖 port 对齐、application/service 拆分、TUI 薄化、runtime 收口、adapter 强化与测试治理
- 这些阶段的细节不再作为主文档主体；如需追溯变更履历，应查看对应阶段报告

### Phase 6：TUI type safety + 文档同步 + 按需优化 (下一阶段)

目标：

- 为 `ChatControllerMixin` 引入 Protocol 约束，提升 TUI 类型安全
- 保持 cron / read model / DI 现状，不做过度拆分
- 在出现真实需求前，不引入新的抽象层

工作项：

1. 定义 `ProjectorHost` Protocol，约束 `ChatControllerMixin` 对 `AlexApp` 的 duck typing 访问
2. 保持 `CronManager` 现状（~558 行），ownership 链路已清晰，暂不拆分
3. 保持 `ChatHistory` + `CronHistoryReadModel` 现状，等出现第二个消费者再增量抽取
4. 保持 `composition.py` 为 wiring 唯一来源，不引入外部 DI container

完成标志：

- `ChatControllerMixin` 的类型访问有 Protocol 约束
- 文档与实现一致，不保留已解决的"重复 wiring"等历史叙事
- 无新抽象层引入，架构纯度与复杂度之间保持平衡

---

## 最终验收标准

当以下条件都满足时，才能称为"架构重构真正完成"：

1. `AgentFacade` 只负责组合与代理，不再承担主业务逻辑
2. `SessionRepository`、`SkillServicePort`、`ToolExecutionContext` 均为真实稳定边界
3. event bus 的语义在文档、实现、测试三处完全一致
4. ✅ TUI 中不再存在横跨命令解析、状态管理、事件投影、通知控制的超级 controller
5. 所有 session 级状态都有显式 owner
6. 所有关键架构约束都有 contract test 或语义测试保护

---

## 一句话总结

当前 Alex 已经是模块化单体 v2.6：主路径的 application service、event bus、TUI projector、tool runtime、session/store 边界、wiring 收口和测试语义都已基本稳定。真正剩余的问题，不再是"大规模拆层"，而是少数类型安全细节和文档同步。

下一阶段的重点：
1. TUI controller 的 Protocol 类型约束
2. 保持 cron / read model / DI 的现状，不过度拆分
3. 文档持续与实现对齐
