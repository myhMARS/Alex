# Alex 模块化重构设计文档

## 文档定位

本文档描述两件事：

1. 我认为 Alex 的理想技术架构应该长什么样
2. 当前实现距离该目标还差哪些边界、抽象、状态模型和执行链路

这不是“当前代码已经重构完成”的验收稿，而是一份面向未来多个迭代的架构蓝图。它优先反映运行时真实结构，其次再定义理想目标和演进路径。

---

## 结论摘要

Alex 已经从早期的”`Agent + TUI + 工具集合`”单体，演进成模块化单体 v2.2：

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

1. `SkillManager` 兼容层尚未完全移除（Phase 5）
2. Agent 内部仍是 direct-call + event 混合模型（orchestrator/cron_handler 直调 chat_service）
3. port contract tests 和 state model tests 尚未补齐

因此，当前最准确的判断是：

- 当前架构：模块化单体 v2.2（runtime model 收口，push_notification 语义清理，factory 化）
- 目标架构：模块化单体 v2.3（SkillManager 移除，contract tests 补齐）

---

## 必须保留的约束

后续所有重构必须保持以下行为不变：

1. 使用 DeepSeek thinking mode 时，所有 `AIMessage` 都必须保留 `reasoning_content`
2. `/help`、`/skills` 等 TUI modal 视图必须拦截文本输入，并通过 `:q` 退出
3. 所有后台任务必须绑定到 Textual 主事件循环，或显式转移到线程池/后台 worker
4. Session 持久化必须保存原始 `BaseMessage` 序列，而不是 UI 视图模型
5. cron 任务本身保持易失，不落盘；只保存执行历史
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
│   ├── orchestrator.py         # TurnOrchestrator — single turn execution
│   ├── cron_handler.py         # CronTurnHandler — cron-triggered LLM replies
│   ├── feedback.py             # FeedbackRecorder (legacy, retained for compat)
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
│   ├── models.py               # Skill dataclass, SkillManager (compat)
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
│   ├── time.py                 # Time tool
│   ├── web_fetch.py            # Web fetch tool
│   ├── web_search.py           # Web search tool
│   └── ports.py                # ToolRegistry/ToolExecutor Protocols
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

### 当前架构已完成的里程碑

以下能力已经可以视为 v2 架构基线：

1. `agent / bus / memory / skill / store / tools / scheduler / tui` 目录边界已经建立
2. `Event -> Command / DomainEvent / UIEvent` 已统一
3. `AsyncEventBus` 已接管主事件传播，角色明确为 event-only bus
4. `SessionPersistence` 已接管 `TurnCompleted` / `CronJobEvent` 的自动持久化
5. `cron` 的 `session_id` 已贯穿调度与订阅回复链路
6. `StreamRenderer` 已把用户 turn 和 cron turn 渲染状态统一
7. 跨会话 cron 污染、运行中 cron 取消、反馈状态泄漏等高风险问题已经修复

**2026-05-25 (Phase 1 语义校正) 新增里程碑：**

8. `store/ports.py` — `SessionRepository` Protocol + `SessionBundle` TypedDict，对齐 bundle 语义
9. `skill/ports.py` — `SkillServicePort` 与真实 `SkillService` 接口完全对齐（10 methods）
10. `agent/ports.py` — 清理漂移的旧 Protocol，`AgentFacade` 保持为 TUI 唯一契约
11. `FeedbackSessionState` — per-session feedback 状态字典替代实例级可变字段

**2026-05-26 (Phase 2 Application Layer 再拆分) 新增里程碑：**

12. `ChatAppService` (`agent/chat_service.py`) — 聊天流、工具执行、图管理、cron 历史
13. `FeedbackAppService` (`agent/feedback_service.py`) — 反馈/反思，注入式依赖
14. `SkillAdminAppService` (`agent/skill_admin_service.py`) — 技能 CRUD/合并
15. `Agent` (`agent/service.py`) — 降为薄 facade，只负责组合注入与代理调用

**2026-05-27 (Phase 3 Projection 与 UI 薄化) 新增里程碑：**

16. `ChatProjector` (`tui/chat_projector.py`) — bus→widget 投影，cron renderer 管理，status bar
17. `SessionViewState` (`tui/view_state.py`) — UI 状态收口，`reset()` 统一入口
18. `NotificationController` (`tui/notification_controller.py`) — toast / feedback 生命周期
19. `controller.py` 从 608 行降至 282 行（-54%）
20. `AlexApp` 成为 wiring center：装配 projector / notifications / view_state

### 当前架构仍存在的核心问题

1. `SkillManager` 兼容层仍在主路径中（`agent/service.py` 和 `agent/chat_service.py` 均 import）
2. Agent 内部仍是 direct-call + event 混合模型（orchestrator/cron_handler 通过 `push_notification` 回调与 chat_service 耦合）
3. Port contract tests 和 state model tests 尚未补齐

---

## 当前架构与理想架构的差距

### 差距总表

| 领域 | 当前架构 | 理想架构 | 差距等级 |
|------|----------|----------|----------|
| Agent / Application | ✅ 薄 facade 组合 5 个 app service，wiring 在 Agent.__init__ | DI container 替代手工 wiring | 低 |
| Session / Store | ✅ `SessionService` 通过 `session_serializer` 访问，不再直接 import store 内部 | - | 已解决 |
| Skill | ✅ `SkillServicePort` 已对齐 `SkillService`，`SkillManager` 为兼容层 | `SkillManager` 完全移除，只用 `SkillService` | 低 |
| Event System | ✅ typed event + event-only bus 语义明确，但 Agent 内仍混合 direct-call | 统一 direct-call application service 模式 | 中 |
| TUI / Projection | ✅ controller 已薄化至 282 行，ChatProjector / SessionViewState / NotificationController 已分离 | - | 已解决 |
| Tool Runtime | ✅ `ToolExecutionContext` 为一等对象，executor 和所有 caller 已对齐 | - | 已解决 |
| Cron / Scheduler | 已稳定可用，但 handler、projection、cancel 语义仍分散 | `CronAppService + SchedulerAdapter + CronProjector` 明确收口 | 中 |
| Feedback / Reflection | ✅ `FeedbackAppService` + `FeedbackSessionState` per-session 字典 | episodes 持久化为独立日志（可选） | 低 |
| Read Models | 主要靠 `ChatHistory` 与即时渲染状态 | 明确 projector + read model 边界 | 中 |
| Tests / Governance | 100 回归测试通过，缺 contract tests 和语义测试 | module contract / state model / concurrency tests 完整 | 中 |

---

## 分模块详细设计路线

下面每一节都按四个维度描述：

1. 当前问题
2. 目标设计
3. 重构路线
4. 验收标准

### 1. Agent / Application Layer ✅ (2026-05-26 Phase 2 完成)

#### 当前状态

`Agent` 已降为薄 facade，5 个 application service 已就位：

- `ChatAppService` (`agent/chat_service.py`) — chat_stream, tool exec, graph, cron history
- `SessionService` (`agent/session_service.py`) — session persistence boundary
- `CronService` (`agent/cron_service.py`) — cron schedule/cancel/lifecycle
- `FeedbackAppService` (`agent/feedback_service.py`) — rating, episode, reflection
- `SkillAdminAppService` (`agent/skill_admin_service.py`) — skill CRUD, merge

`Agent.__init__` 现在只负责组合注入和 wiring，业务逻辑入口均在 application service。

#### 验收状态

1. ✅ `service.py` 不再包含大段聚合逻辑
2. ✅ `Agent` 只负责 facade 和依赖装配
3. ✅ 所有业务入口均落在 application service，而不是 facade 本体

### 2. Session / Store Boundary

#### 当前问题

虽然已经有 `SessionService`，但当前边界仍不理想：

- `SessionService` 直接依赖 `SessionPersistence`
- `restore_history()` 仍直接 import `deserialize_message`
- `store/ports.py` 定义的 `SessionStore` 与真实 bundle 语义不一致

这说明 session 边界现在更像“文件级包装”，还不是成熟 repository。

#### 目标设计

拆成三个稳定对象：

1. `SessionRepository`
   负责 load/save/list/delete bundle
2. `SessionSerializer`
   负责 `BaseMessage <-> dict` roundtrip
3. `SessionAppService`
   负责 restore / rehydrate / clear / metadata query

目标接口：

```python
class SessionBundle(TypedDict):
    session_id: str
    created_at: str
    first_message: str
    messages: list[BaseMessage]
    cron_history: list[dict]

class SessionRepository(Protocol):
    def load_bundle(self, session_id: str) -> SessionBundle | None: ...
    def save_bundle(self, bundle: SessionBundle) -> None: ...
    def append_cron_record(self, session_id: str, record: dict) -> None: ...
    def list_sessions(self) -> list[dict]: ...
```

#### 重构路线

Phase A:

1. 在 `store/ports.py` 中把 `SessionStore` 改成 bundle 语义
2. 新增 `SessionBundle` 类型
3. 让 `SessionPersistence` 实现该 port

Phase B:

1. 从 `store/session.py` 中拆出 `session_serializer.py`
2. 让 `SessionService` 只依赖 repository port，不再 import `deserialize_message`

Phase C:

1. 新建 `agent/session_app_service.py`
2. 统一 `load_session()`、`restore_history()`、`clear_history()`、`list_sessions()`
3. 删除 `SessionService` 中“只是包装静态方法”的设计

#### 验收标准

1. `agent` 不再 import `store.session` 细节
2. `store/ports.py` 与真实实现一致
3. session contract test 可以直接跑在 port 上

### 3. Skill 模块 ✅ (Phase 1 ports 对齐, Phase 2 SkillAdminAppService)

#### 当前状态

- `skill/ports.py` — `SkillServicePort` Protocol 与真实 `SkillService` 完全对齐（10 methods）
- `SkillAdminAppService` (`agent/skill_admin_service.py`) — 封装 `SkillManager`，提供 list/delete/deprecate/merge/load_skill
- `SkillManager` — 保留为 `SkillService` 子类的兼容层，带 lazy default construction
- `Agent` 通过 `SkillAdminAppService` 间接访问 `SkillManager`

#### 剩余工作 (Phase 5)

1. 从主路径完全移除 `SkillManager`
2. 强化 `SkillStore` 的原子写与坏数据处理

### 4. Event System：Command / Event 语义 ✅ (Phase 1 语义明确)

#### 当前状态

已明确选择 **方案 B**（direct-call application service + event bus 仅做状态传播）：

- `AsyncEventBus` 角色明确为 event-only bus，不做 command dispatching
- `UserTurnRequested` 仅作为 observability event 发布
- `chat_stream()` 直接调用 `ChatAppService`（通过 Agent facade）
- `CronTurnHandler` 触发入口在 `push_notification()` 中（混合模式，待 Phase 4 收口）

#### 剩余工作 (Phase 4)

1. 统一 `push_notification()` 语义，避免 “publish + create_task 旁路处理”
2. CronTurnHandler 触发入口只能保留一条

### 5. TUI / Projection Layer ✅ (2026-05-27 Phase 3 完成)

#### 当前状态

Phase 3 已将 TUI controller 拆分为四个独立对象：

1. `ChatProjector` (`tui/chat_projector.py`, 300 lines)
   - Bus → widget 事件投影（11 个 cron/skill event handler）
   - `_cron_renderers` dict 管理
   - `refresh_status_bar()` / `trim_chat_view()` 静态工具
   - `format_cron_page()` / `persist_cron_record()` cron history read model

2. `SessionViewState` (`tui/view_state.py`, 28 lines)
   - `page_mode`、`showing_session_list`、`session_options`
   - `pending_feedback_turn_id`、`last_response_rated`
   - `reset()` 方法在 session 切换时统一调用

3. `NotificationController` (`tui/notification_controller.py`, 98 lines)
   - `show_toast()` / `dismiss_toast()` / `format_reflect_toast()`
   - `show_feedback_prompt()` / `dismiss_feedback()` / `rate_response()`
   - toast widget / feedback widget 生命周期

4. `ChatControllerMixin` (`tui/controller.py`, 282 lines, was ~600)
   - 仅保留命令分发、page 管理、session 生命周期、toggles
   - 所有投影/通知职责已委托给 ChatProjector / NotificationController

#### 验收状态

1. ✅ `controller.py` 从 ~600 行降至 282 行（-54%，目标 < 300）
2. ✅ UI 状态变更路径统一（`SessionViewState.reset()`）
3. ✅ session 切换时只有一个地方负责 reset UI state
4. ✅ modal / resume 路径仅依赖 controller + view_state

### 6. Cron / Scheduler

#### 当前问题

当前 cron 功能已经稳定，但架构上仍分散在：

- `CronService`
- `CronManager`
- `CronTurnHandler`
- TUI cron projector
- store 对 `CronJobEvent` 的持久化订阅

这意味着“一个 cron 完整生命周期”没有单一抽象中心。

#### 目标设计

建立三个明确对象：

1. `CronAppService`
   对 application layer 暴露 schedule / cancel / query / subscribe
2. `SchedulerAdapter`
   对 APScheduler 做纯适配
3. `CronExecutionCoordinator`
   负责 `CronJobEvent -> subscribed reply -> turn persistence` 的执行链

#### 重构路线

Phase A:

1. 将 `CronService` 从“薄包装”升级为真正的 `CronAppService`
2. `Agent` 不再直持有 `_cron`

Phase B:

1. 将 `_schedule_aps()`、cancel semantics、running task tracking 视为 adapter 内部实现
2. `CronManager` 只对外暴露 scheduler contract

Phase C:

1. 将 `CronTurnHandler` 重命名或抽象为 `CronExecutionCoordinator`
2. 明确它的输入是 `CronExecutionRequest`，而不是直接绑在 `CronJobEvent` 上

#### 验收标准

1. cron 生命周期从 schedule 到 subscribed reply 有唯一主抽象
2. TUI 只消费投影事件，不参与业务决策
3. store 只消费持久化事件，不参与执行决策

### 7. Tool Runtime (Phase 1 partial, Phase 4 完成剩余)

#### 当前状态

`ToolExecutor.execute(session_id, ...)` 已接受 `session_id`，但尚未提升为正式 `ToolExecutionContext`。这在 Phase 1 中标记为后续工作。

#### 剩余工作 (Phase 4)

1. 引入 `ToolExecutionContext` dataclass
2. `ToolExecutor.execute(ctx, name, args)` 签名升级
3. `cron_history`、未来 session-aware tools 统一使用 context

### 8. Feedback / Reflection ✅ (2026-05-26 Phase 2 完成)

#### 当前状态

`FeedbackAppService` (`agent/feedback_service.py`) 已实现：

- `FeedbackSessionState` dataclass — per-session turn_count, reflecting flag, episodes
- `dict[session_id, FeedbackSessionState]` — 多 session 状态隔间
- `_state()` 方法自动创建/返回当前 session 状态
- `set_session_id()` — 新 session 自动创建新 state
- `reset_session_state(session_id)` — 显式清除（用于 `/clear`）

原 `FeedbackRecorder` (`agent/feedback.py`) 保留为兼容参考，但主路径已使用 `FeedbackAppService`。

#### 验收状态

1. ✅ feedback 状态以 session 为键，而不是单实例字段
2. ✅ 会话切换逻辑通过 `set_session_id` / `reset_session_state` 统一管理
3. ✅ 10/10 feedback 回归测试通过

### 9. Read Models / Projection

#### 当前问题

当前 `ChatHistory` 已经很干净，但它仍然同时是：

- TUI 的主要 read model
- renderer finalize 的落点
- cron history 的内存缓存

随着 UI 能力增加，它仍然会继续变大。

#### 目标设计

把读模型拆成：

1. `ChatTimeline`
2. `CronHistoryReadModel`
3. `SessionListReadModel`
4. `FeedbackReadModel`

`ChatProjector` 根据 event 更新这些模型，再由 TUI 读取。

#### 重构路线

Phase A:

1. 将 `ChatHistory` 保留为组合容器，而不是所有字段都挂在一个类上
2. 引入 `CronHistoryReadModel`

Phase B:

1. `StreamRenderer.finalize()` 输出标准 `RenderedTurn`
2. projector 决定如何写入 timeline

#### 验收标准

1. read model 与 renderer 不再相互缠绕
2. cron history 不再是 timeline 的附属字段

### 10. Testing / Governance

#### 当前问题

当前已有功能回归测试，但还缺少三类关键测试：

1. port contract tests
2. state model tests
3. concurrency semantics tests

#### 目标设计

必须补齐：

1. `SessionRepository` contract tests
2. `SkillServicePort` contract tests
3. `ToolExecutionContext` 注入 tests
4. `EventBus` 串行语义 tests
5. `Cron cancel semantics` tests
6. `TUI projector / view state` tests

#### 重构路线

Phase A:

1. 为 `store/ports.py` 与真实 adapter 写 contract tests
2. 为 `skill/ports.py` 与真实 service 写 contract tests

Phase B:

1. 为 session 切换、feedback state、cron render projector 写 state tests
2. 为 bus 明确“单消费者串行”写语义测试

Phase C:

1. 将架构文档中的每一条关键语义都映射到测试用例

#### 验收标准

1. 每个核心 port 都有 contract test
2. 每个关键并发 / 会话语义都有测试
3. 文档与测试可以互相印证

---

## 分阶段路线图

### Phase 1：语义校正 ✅ (2026-05-25 完成)

目标：

- 让文档、port、代码先说同一种语言

工作项：

1. ✅ 修正 `store/ports.py` — `SessionStore` → `SessionRepository` + `SessionBundle`
2. ✅ 修正 `skill/ports.py` — `SkillServicePort` 与真实 `SkillService` 对齐
3. ✅ 明确 `EventBus` 仅为 event bus（文档 + agent/ports.py 清理）
4. ✅ 删除 agent/ports.py 中漂移的旧 Protocol

完成标志：

- 文档与代码语义对齐 ✅
- port 不再漂移 ✅

### Phase 2：Application Layer 再拆分 ✅ (2026-05-26 完成)

目标：

- 让 `Agent` 从”大聚合根”继续瘦身

工作项：

1. ✅ 新建 `ChatAppService` (`agent/chat_service.py`)
2. ✅ 保留并增强 `SessionService` (`agent/session_service.py`)
3. ✅ 保留并增强 `CronService` (`agent/cron_service.py`)
4. ✅ 新建 `FeedbackAppService` (`agent/feedback_service.py`)
5. ✅ 新建 `SkillAdminAppService` (`agent/skill_admin_service.py`)

完成标志：

- `service.py` 只剩 facade 与 wiring ✅
- 100/100 回归测试通过 ✅

### Phase 3：Projection 与 UI 薄化 ✅ (2026-05-27 完成)

目标：

- 让 TUI 不再是第二个 application layer

工作项：

1. ✅ 从 `controller.py` 拆出 `chat_projector.py`（bus handler → widget 更新）
2. ✅ 创建 `tui/view_state.py`，收口 `_page_mode`、`_showing_session_list`、`_pending_feedback_turn_id` 等状态
3. ✅ 从 `controller.py` 拆出 `notification_controller.py`（toast、feedback prompt、status bar）
4. ✅ 将 `_cron_renderers` 管理逻辑移入 projector
5. ✅ Session 切换时只重置 `SessionViewState`

完成标志：

- ✅ `controller.py` 从 608 行降至 282 行（-54%）
- ✅ UI 状态变更路径统一（`SessionViewState.reset()`）
- ✅ 92/92 回归测试通过

### Phase 4：Runtime 与状态模型收口 ✅ (2026-05-28 完成)

目标：

- 建立稳定的 session-scoped 状态和 tool runtime

工作项：

1. ✅ `ToolExecutionContext` 已为一等运行时上下文（`tools/ports.py` 定义，`ToolExecutor` 使用，`chat_service:execute_tool_action` 创建）
2. ✅ `CronHistoryReadModel` 从 `ChatHistory` 独立（`tui/cron_history.py`）
3. ✅ `SessionService` 不再直接 import `deserialize_message` — 通过 `store/session_serializer.py` 中间层
4. ✅ Agent wiring 工厂化 — `agent/factory.py:create_agent()` 
5. ✅ `push_notification()` 语义收口 — `Agent.push_notification` 为单一发布入口，cron reply 走显式 `dispatch_cron_reply`

完成标志：

- ✅ `SessionService` 不直接依赖 `store.session` 内部实现
- ✅ cron_history 不再是 ChatHistory 的附属字段
- ✅ push_notification 路径从 2 条合并为 1 条
- ✅ 92/92 回归测试通过

### Phase 5：Adapter 强化与测试治理 (计划: 2026-05-29)

目标：

- 让模块边界真正可替换、可演化

工作项：

1. 从主路径完全移除 `SkillManager` 兼容层，统一使用 `SkillService`
2. 强化 `SkillStore` 的原子写与坏数据处理
3. 增加 port contract tests：`SessionRepository`、`SkillServicePort`
4. 增加 state model tests：session 切换、feedback state、cron cancel
5. 增加 event bus 串行语义 tests

完成标志：

- `SkillManager` 不再出现在任何 import 路径中
- 每个核心 port 都有 contract test
- 技术债从”边界不硬”转为”业务优化空间”

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

当前 Alex 已经是模块化单体 v2.2：Application Layer 拆分为 5 个独立 service，TUI 拆分为 4 个薄对象，ToolExecutionContext 为一等运行时上下文，SessionSerializer 消除 store 边界泄露，CronHistoryReadModel 独立，push_notification 单一发布路径，Agent wiring 工厂化。

下一阶段（2026-05-29）的重点是 **Phase 5：Adapter 强化与测试治理**：
1. 从主路径完全移除 `SkillManager` 兼容层
2. 增加 port contract tests：`SessionRepository`、`SkillServicePort`
3. 增加 state model tests：session 切换、feedback state、cron cancel
