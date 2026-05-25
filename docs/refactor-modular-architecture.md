# Alex 模块化重构设计文档

## 文档定位

本文档描述两件事：

1. 我认为 Alex 的理想技术架构应该长什么样
2. 当前实现距离该目标还差哪些边界、抽象、状态模型和执行链路

这不是“当前代码已经重构完成”的验收稿，而是一份面向未来多个迭代的架构蓝图。它优先反映运行时真实结构，其次再定义理想目标和演进路径。

---

## 结论摘要

Alex 当前已经从早期的”`Agent + TUI + 工具集合`”单体，演进成一个可运行的模块化单体 v1：

- 目录结构已经按模块拆分
- typed event 已经统一
- session、cron、stream 渲染等关键链路已经稳定
- 多个高风险行为问题已经修复

2026-05-25 迭代完成了 Phase 1 语义校正的核心工作：
- `store/ports.py` — 从 `SessionStore` 重命名为 `SessionRepository`，改为 bundle 语义对齐 `SessionPersistence`
- `skill/ports.py` — 与 `SkillService` 真实接口重新对齐，移除漂移的旧方法签名
- `ToolExecutionContext` — 引入正式运行时上下文 dataclass，替代裸 `session_id`
- `FeedbackSessionState` — 每 session 独立 feedback 状态，替代旧实例级可变字段

当前距离”最佳长期演进架构”的主要差距集中在：
1. application layer 仍然不纯，`Agent` facade 还承担了过多 wiring 与具体实现依赖
2. agent/ports.py 中的 `SkillServicePort` 已重新指向 `skill/ports.py` 的一致接口
3. command bus 与 event bus 语义已明确分离（event-only bus），但 Agent 内部仍是 direct-call + event 的混合模型
4. TUI controller 仍承担 session 切换、cron projection、feedback UI 状态等多类职责

因此，当前最准确的判断是：

- 当前架构：模块化单体 v1.1，port/adapter 语义已对齐，runtime model 已开始建立
- 目标架构：模块化单体 v2，application、domain、adapter、projection、runtime 五层职责清晰

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
│   ├── service.py
│   ├── session_service.py
│   ├── cron_service.py
│   ├── orchestrator.py
│   ├── cron_handler.py
│   ├── feedback.py
│   ├── prompt.py
│   └── ports.py
├── bus/
│   ├── events.py
│   └── in_memory.py
├── memory/
│   ├── base.py
│   ├── buffer.py
│   └── ports.py
├── skill/
│   ├── models.py
│   ├── service.py
│   ├── repository.py
│   ├── matcher.py
│   ├── reflector.py
│   ├── evolution.py
│   └── ports.py
├── store/
│   ├── session.py
│   ├── session_adapter.py
│   └── ports.py
├── tools/
│   ├── cron.py
│   ├── executor.py
│   ├── registry.py
│   ├── time.py
│   ├── web_fetch.py
│   ├── web_search.py
│   └── ports.py
├── scheduler/
│   └── manager.py
├── tui/
│   ├── app.py
│   ├── controller.py
│   ├── presenter.py
│   ├── view_models.py
│   └── stream_renderer.py
└── prompts/
```

### 当前真实关系

```text
TUI
  -> AgentFacade
      -> SessionService
      -> CronService
      -> PromptAssembler
      -> TurnOrchestrator
      -> CronTurnHandler
      -> FeedbackRecorder
      -> ToolRegistry + ToolExecutor
      -> SkillManager / SkillService
      -> Memory
      -> EventBus
          -> SessionPersistence
          -> TUI subscribers / projectors
```

### 当前架构已完成的里程碑

以下能力已经可以视为 v1 架构基线：

1. `agent / bus / memory / skill / store / tools / scheduler / tui` 目录边界已经建立
2. `Event -> Command / DomainEvent / UIEvent` 已统一
3. `AsyncEventBus` 已接管主事件传播
4. `SessionPersistence` 已接管 `TurnCompleted` / `CronJobEvent` 的自动持久化
5. `cron` 的 `session_id` 已贯穿调度与订阅回复链路
6. `StreamRenderer` 已把用户 turn 和 cron turn 渲染状态统一
7. 跨会话 cron 污染、运行中 cron 取消、反馈状态泄漏、反思角色映射、`web_search` 阻塞等高风险问题已经修复

**2026-05-25 新增里程碑（Phase 1 语义校正）：**

8. `store/ports.py` — `SessionRepository` Protocol 已与 `SessionPersistence` 真实接口对齐（bundle 语义）
9. `skill/ports.py` — `SkillServicePort` 已与 `SkillService` 真实接口对齐，移除旧漂移方法
10. `ToolExecutionContext` — 正式 runtime model 已引入并替换 `ToolExecutor.execute(session_id, ...)` 的裸字符串传递
11. `FeedbackSessionState` — per-session feedback 状态字典已替代 `FeedbackRecorder` 的实例级可变字段

### 当前架构仍存在的核心问题

1. `Agent` 仍是 application 层的大聚合对象
2. `SessionService` 仍直接 depend on `SessionPersistence`，尚未升级为完整 repository 模式
3. `EventBus` 只有 event 语义，Agent 内部仍是 direct-call + event 的混合模型
4. TUI controller 仍同时承担命令解析、cron projection、feedback UI 状态、toast 生命周期

---

## 当前架构与理想架构的差距

### 差距总表

| 领域 | 当前架构 | 理想架构 | 差距等级 |
|------|----------|----------|----------|
| Agent / Application | 一个大 facade 聚合多服务，仍直连具体实现 | 多个 application service 分治职责 | 高 |
| Session / Store | `SessionService` 只是薄包装，store ports 漂移 | `SessionRepository + SessionSerializer + SessionAppService` 明确分层 | 高 |
| Skill | 已拆出 `SkillService`，但 `SkillManager` 兼容层仍在主路径，ports 漂移 | `SkillApplication + SkillDomain + SkillRepository` 契约统一 | 高 |
| Event System | typed event 完整，但 command handling 仍是半直调半事件 | command / event 语义一致，执行入口唯一 | 高 |
| TUI / Projection | controller 同时处理命令、投影、会话切换、toast、feedback | controller 薄化，projector 和 view state 分离 | 中 |
| Tool Runtime | `session_id` 已透传但未形成真正上下文注入模型 | `ToolExecutionContext` 为一等对象 | 中 |
| Cron / Scheduler | 已稳定可用，但 handler、projection、cancel 语义仍分散 | `CronAppService + SchedulerAdapter + CronProjector` 明确收口 | 中 |
| Feedback / Reflection | 已修会话泄漏，但状态仍是 recorder 内部可变字段 | 每 session 独立 feedback state，可显式 reset/persist | 中 |
| Read Models | 主要靠 `ChatHistory` 与即时渲染状态 | 明确 projector + read model 边界 | 中 |
| Tests / Governance | 有回归测试，但缺 contract tests 和语义级测试 | module contract / state model / concurrency tests 完整 | 中 |

---

## 分模块详细设计路线

下面每一节都按四个维度描述：

1. 当前问题
2. 目标设计
3. 重构路线
4. 验收标准

### 1. Agent / Application Layer

#### 当前问题

当前 `alex/agent/service.py` 已经比早期轻很多，但仍然集中承担：

- application facade
- tool registry 生命周期
- cron service 生命周期
- graph 构建
- skill CRUD 暴露
- session context 广播
- feedback / reflection 入口

这意味着新功能仍然很容易继续堆进 `Agent`。

#### 目标设计

把当前单一 `Agent` facade 继续拆成 5 个 application service：

1. `ChatAppService`
   负责用户 turn、流式执行、turn 生命周期
2. `SessionAppService`
   负责 load / restore / clear / list sessions
3. `CronAppService`
   负责 schedule / cancel / query / subscribe reply
4. `FeedbackAppService`
   负责 rating、episode、reflect trigger
5. `SkillAdminAppService`
   负责 list / delete / deprecate / merge skills

`AgentFacade` 最终只保留：

- 组合注入
- 少量兼容入口
- 面向 TUI 的统一门面

#### 重构路线

Phase A:

1. 新建 `agent/chat_service.py`
2. 将 `chat_stream()`、`last_turn_result`、prompt 刷新逻辑迁入 `ChatAppService`
3. `Agent` 只保留代理调用

Phase B:

1. 新建 `agent/skill_admin_service.py`
2. 将 `list_skills()`、`delete_skill()`、`deprecate_skill()`、`merge_skills()` 迁出 `Agent`
3. 让 `TUI` 通过 facade 调 skill admin service，而不是直碰 `_skills`

Phase C:

1. 新建 `agent/context_service.py`
2. 将 `set_session_context()` 的子组件广播行为迁出 `Agent`
3. 所有 session-scoped service 改为向 context service 注册

#### 验收标准

1. `service.py` 不再包含大段聚合逻辑
2. `Agent` 只负责 facade 和依赖装配
3. 所有业务入口均落在 application service，而不是 facade 本体

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

### 3. Skill 模块

#### 当前问题

当前 skill 已经比旧架构健康，但仍有三类债：

1. `SkillManager` 兼容层仍在主路径
2. `skill/ports.py` 与 `skill/service.py` 方法签名不一致
3. repository 对坏数据过于宽容，容易静默退化

#### 目标设计

把 skill 模块明确分成三层：

1. `SkillApplicationService`
   负责管理入口与 orchestration
2. `SkillDomainService`
   负责 match / reflect / evolve / feedback rules
3. `SkillRepository`
   负责持久化与模板文件同步

理想结构：

```text
skill/
  application.py
  domain.py
  repository.py
  matcher.py
  reflector.py
  evolution.py
  ports.py
  models.py
```

#### 重构路线

Phase A:

1. 重写 `skill/ports.py`，让它完全匹配当前真实服务接口
2. `Agent` 改依赖 `SkillServicePort` 而不是 `SkillManager`

Phase B:

1. 将 `SkillManager` 降级为纯兼容别名，逐步从主路径移除
2. 把 `reflect()`、`list_all()`、`get_skill_by_name()`、`record_usage()`、`merge_skills()` 统一收口到 `SkillApplicationService`

Phase C:

1. 强化 `SkillStore` 的错误处理
2. 引入原子写文件策略
3. 损坏数据时 fail-fast 或明确告警，而不是静默吞错

#### 验收标准

1. `skill/ports.py` 与真实实现完全一致
2. `Agent` 和 `TUI` 都不再依赖 `SkillManager`
3. repository 损坏场景有明确行为和测试

### 4. Event System：Command / Event 语义收口

#### 当前问题

当前是典型的“半命令总线”状态：

- `UserTurnRequested` 已发布，但只用于观测，不驱动执行
- `CronJobEvent` 既用于状态传播，也被业务链路拿来触发旁路处理
- `AsyncEventBus` 当前是单消费者串行总线，但过去文档和预期曾把它描述成跨 session 并发总线

#### 目标设计

这部分必须做二选一，而不是继续模糊：

方案 A：显式双总线

- `CommandBus` 负责执行入口
- `EventBus` 负责状态传播

方案 B：保持 direct-call application service

- TUI / scheduler 直接调用 app service
- bus 只负责 domain / UI events

推荐 **方案 B**。原因：

1. 当前是单进程 TUI 应用，不需要为了形式引入 command bus
2. direct-call 更容易调试和测试
3. 只要边界够硬，application service + event bus 已足够

#### 重构路线

Phase A:

1. 在文档中明确：当前 bus 是 event bus，不是 command bus
2. 删除“用户 turn 已命令事件化”的误导表述

Phase B:

1. `chat_stream()` 改为直接调用 `ChatAppService.run_turn()`
2. `UserTurnRequested` 仅作为 observability event

Phase C:

1. 统一 `push_notification()` 的语义，避免“publish + create_task 旁路处理”
2. `CronTurnHandler` 的触发入口要么由 `CronAppService` 直接调用，要么由专门 subscriber 触发，二者只能保留一条

#### 验收标准

1. 文档、代码、心智模型都明确 bus 的角色
2. 不再存在“发了 command event 但实际没人消费”的伪边界
3. cron 订阅回复只有一条正式执行链

### 5. TUI / Projection Layer

#### 当前问题

`ChatControllerMixin` 仍然同时承担：

- 命令解析
- 总线订阅 handler
- 会话切换
- cron projector
- feedback UI 状态
- toast 生命周期

这在功能上可用，但会让 TUI 继续成为复杂状态中心。

#### 目标设计

把 TUI 分成四类对象：

1. `InputRouter`
   只负责命令解析和输入闸门
2. `ChatProjector`
   只负责将事件投影成 `ChatHistory` / widget 更新
3. `SessionViewState`
   只负责 `_page_mode`、`_pending_feedback_turn_id`、`_cron_renderers` 等状态
4. `NotificationController`
   只负责 toast、feedback prompt、status bar

#### 重构路线

Phase A:

1. 将 bus handlers 从 `controller.py` 拆到 `chat_projector.py`
2. 将 `_page_mode`、`_showing_session_list`、`_session_options` 收口到 view state

Phase B:

1. 将 `_show_toast()`、`_dismiss_feedback()`、`_show_feedback_prompt()` 拆到 notification controller
2. 让 session 切换时只重置 `SessionViewState`

Phase C:

1. `AlexApp` 只负责装配 Textual widgets 和绑定 controller
2. `ChatControllerMixin` 消失或降为薄协调器

#### 验收标准

1. TUI 中任何一个类的职责不再横跨命令、投影、状态、通知四类
2. 会话切换时只有一个地方负责 reset UI state
3. modal、resume、cron render 的测试可以只打 projector / state 层

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

### 7. Tool Runtime ✅ (2026-05-25 Phase A 完成)

#### 当前问题

~~`ToolExecutor.execute(session_id, ...)` 已接受 `session_id`，但还没有把它提升为正式运行时上下文。~~ **已解决。**

#### 目标设计 ✅ 已实现

`ToolExecutionContext` dataclass 已创建于 `tools/ports.py`：

```python
@dataclass
class ToolExecutionContext:
    session_id: str
    turn_id: str | None = None
    source: str = "user"
    metadata: dict[str, Any] = field(default_factory=dict)
```

工具执行入口已改为：

```python
async def execute(self, ctx: ToolExecutionContext, name: str, args: dict) -> str:
    ...
```

#### 重构路线

Phase A: ✅ 已完成

1. ✅ 新建 `ToolExecutionContext` 于 `tools/ports.py`
2. ✅ `Agent.execute_tool_action()` 构造 context
3. ✅ `ToolExecutor.execute()` 签名为 `(ctx, name, args)`
4. ✅ 测试全部更新

Phase B: (后续)

1. `ToolExecutor` 支持为 tool 注入 context
2. 为内置工具定义是否接收 context 的统一规则

Phase C: (后续)

1. `cron_history`、future session-aware tools、审计日志统一使用 context

#### 验收状态

1. ✅ 不再只传裸 `session_id`
2. ✅ tools runtime 的契约与实现一致
3. ⬜ 新工具不需要再通过闭包或宿主对象偷拿 session 信息

### 8. Feedback / Reflection ✅ (2026-05-25 Phase A+B 完成)

#### 当前问题

~~虽然跨会话泄漏已修，但 recorder 仍然是”一个对象里塞所有 session 当前状态”的模式。~~ **已解决。**

#### 目标设计 ✅ 已实现

每 session 一份 feedback state：

```python
@dataclass
class FeedbackSessionState:
    turn_count: int = 0
    reflecting: bool = False
    episodes: list[dict] = field(default_factory=list)
```

`FeedbackRecorder` 管理：

- `dict[session_id, FeedbackSessionState]` (via `_sessions` dict)
- `_state()` 方法自动创建/返回当前 session 的状态
- `set_session_id()` 重置新 session 的计数器和 episodes
- `reset_session_state(session_id)` 显式清除（用于 `/clear`）

#### 重构路线

Phase A: ✅ 已完成

1. ✅ 引入 `FeedbackSessionState` dataclass
2. ✅ `FeedbackRecorder` 改为 `dict[session_id, FeedbackSessionState]` 模式
3. ✅ 所有内部访问通过 `_state()` 方法

Phase B: ✅ 已完成

1. ✅ `set_session_id()` 重置新 session 的 counters + episodes
2. ✅ `reset_session_state()` 新增，TUI 可通过此接口显式清除

Phase C: (后续)

1. 若未来需要，可选择将 feedback episodes 持久化为独立调试日志

#### 验收状态

1. ✅ feedback 状态以 session 为键，而不是 recorder 单实例字段
2. ✅ 会话切换逻辑通过 `set_session_id` / `reset_session_state` 统一管理
3. ✅ 10/10 feedback 测试通过

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

1. ✅ 修正 `store/ports.py` — `SessionStore` → `SessionRepository` + `SessionBundle` TypedDict，对齐 bundle 语义
2. ✅ 修正 `skill/ports.py` — `SkillServicePort` 与真实 `SkillService` 接口对齐
3. ✅ 明确 `EventBus` 仅为 event bus（文档 + agent/ports.py 清理）
4. ✅ 删除 agent/ports.py 中漂移的旧 `SkillServicePort`，改为 re-export `skill/ports.py`

完成标志：

- 文档与代码语义对齐 ✅
- port 不再漂移 ✅
- store/skill/tools 三个核心 port 文件与对应实现一致 ✅

### Phase 2：Application Layer 再拆分

目标：

- 让 `Agent` 从“大聚合根”继续瘦身

工作项：

1. 新建 `ChatAppService`
2. 新建 `SessionAppService`
3. 新建 `CronAppService`
4. 新建 `FeedbackAppService`
5. 新建 `SkillAdminAppService`

完成标志：

- `service.py` 只剩 facade 与 wiring

### Phase 3：Runtime 与状态模型收口

目标：

- 建立稳定的 session-scoped 状态和 tool runtime

工作项：

1. 引入 `ToolExecutionContext`
2. 引入 `FeedbackSessionState`
3. 引入 `CronHistoryReadModel`
4. 将 session reset 行为集中管理

完成标志：

- session 级状态可枚举、可重置、可测试

### Phase 4：Projection 与 UI 薄化

目标：

- 让 TUI 不再是第二个 application layer

工作项：

1. 拆出 `InputRouter`
2. 拆出 `ChatProjector`
3. 拆出 `SessionViewState`
4. 拆出 `NotificationController`

完成标志：

- `controller.py` 明显缩短
- UI 状态变更路径统一

### Phase 5：Adapter 强化与测试治理

目标：

- 让模块边界真正可替换、可演化

工作项：

1. 强化 `SkillStore` 的原子写与坏数据处理
2. 完善 scheduler / bus / tool runtime 语义测试
3. 增加 contract tests

完成标志：

- 技术债从“边界不硬”转为“业务优化空间”

---

## 最终验收标准

当以下条件都满足时，才能称为“架构重构真正完成”：

1. `AgentFacade` 只负责组合与代理，不再承担主业务逻辑
2. `SessionRepository`、`SkillServicePort`、`ToolExecutionContext` 均为真实稳定边界
3. event bus 的语义在文档、实现、测试三处完全一致
4. TUI 中不再存在横跨命令解析、状态管理、事件投影、通知控制的超级 controller
5. 所有 session 级状态都有显式 owner
6. 所有关键架构约束都有 contract test 或语义测试保护

---

## 一句话总结

当前 Alex 已经不是“乱成一团的单体”，但离最佳长期演进架构还有一段距离。

下一阶段重构的重点，不是继续“拆目录”，而是把以下三件事做硬：

1. 让 application / domain / adapter / projection 四层真正分开
2. 让 port / service / runtime 说同一种语言
3. 让 session 级状态成为一等模型，而不是散落在各个对象里的字段
