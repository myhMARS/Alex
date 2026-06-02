# 类型化消息系统 (`alex/kernel/`)

## 设计思路

Alex 的消息系统定义在 `alex/kernel/bus.py` 和 `alex/kernel/contracts/` 中。所有跨模块通信通过 `MessageBus` Protocol 完成，支持三种消息语义：

| 语义 | 基类 | 通信模式 | 用途 |
|------|------|---------|------|
| **Event** | `Event` | 广播 pub/sub，fire-and-forget | 状态变更通知 |
| **Command** | `Command(Event)` | 点对点，可选 ack | 行为请求 |
| **Request/Reply** | `Request[T]` | 点对点，返回类型安全值 | 能力调用 |

`Event` 和 `Command` 继承自同一基类，共享 `event_id` / `session_id` / `turn_id` / `source` / `ts` / `trace_id` 字段。`Request[T]` 独立设计，携带 `_correlation_id` 用于 reply 匹配。

## MessageBus Protocol

```python
class MessageBus(Protocol):
    # 生命周期
    async def start(self) -> None: ...
    async def shutdown(self) -> None: ...

    # Event plane (广播)
    def publish(self, event: Event) -> None: ...
    async def subscribe(self, event_type: type, handler: EventHandler) -> None: ...
    async def unsubscribe(self, event_type: type, handler: EventHandler) -> None: ...

    # Request plane (点对点，有返回值)
    async def request(self, req: Request[T], *, timeout: float = 30.0) -> T: ...
    def provide(self, request_type: type, handler: ReqHandler) -> None: ...
```

- `publish()` → 所有订阅该类型的 handler 并发执行
- `request()` → 发送到唯一的 provider handler，等待返回值；超时 / 无 handler 抛 `CapabilityTimeout` / `CapabilityUnavailable`
- `provide()` → 注册 request handler（一种类型只能有一个）

## 契约组织

所有跨模块消息类型定义在 `alex/kernel/contracts/`，按领域分文件：

```
alex/kernel/contracts/
├── __init__.py    # 统一导出
├── chat.py        # 对话契约
├── tools.py       # 工具契约
├── skills.py      # 技能契约
├── memory.py      # 记忆契约
├── session.py     # 会话契约
└── cron.py        # 定时任务契约
```

### 对话契约 (`chat.py`)

| 类型 | 语义 | 关键字段 | 发布者 → 订阅者 |
|------|------|---------|----------------|
| `UserTurnRequested` | Command | `user_text` | TUI → Agent |
| `TurnStarted` | Event | `kind` ("user"/"cron") | Agent → TUI, Store |
| `TurnCompleted` | Event | `messages`, `content`, `thinking` | Agent → Store, TUI |
| `TurnFailed` | Event | `error` | Agent → TUI |
| `ThinkingUpdated` | Event | `delta`, `stream_id` | Agent → TUI |
| `TokenEmitted` | Event | `delta`, `stream_id` | Agent → TUI |
| `FeedbackSubmitted` | Command | `positive` | TUI → Skill |

### 工具契约 (`tools.py`)

| 类型 | 语义 | 关键字段 | 发布者 → 订阅者 |
|------|------|---------|----------------|
| `GetToolCatalog` | Request[list[ToolSpec]] | — | Agent → Tools |
| `ExecuteTool` | Request[ToolResult] | `name`, `args`, `ctx` | Agent → Tools |
| `RegisterTool` | Request[None] | `name`, `description`, `json_schema`, `callable_ref` | Skill → Tools |
| `UnregisterTool` | Request[None] | `name` | — → Tools |
| `InvokeProviderTool` | Request[ToolResult] | `provider`, `name`, `args` | Tools → MCP |
| `ToolsProvided` | Event | `provider`, `specs` | MCP, Plugin → Tools |
| `ToolStarted` | Event | `tool_id`, `tool_name`, `tool_input` | Tools → TUI |
| `ToolFinished` | Event | `tool_id`, `output` | Tools → TUI |
| `ToolApprovalRequested` | Event | `req_id`, `tool_name`, `preview` | Tools → TUI |
| `ToolApprovalResolved` | Event | `req_id`, `granted`, `remember` | TUI → Tools |

### 技能契约 (`skills.py`)

| 类型 | 语义 | 关键字段 | 发布者 → 订阅者 |
|------|------|---------|----------------|
| `RetrieveSkills` | Request[list[SkillCard]] | `query`, `top_k` | Agent → Skill |
| `LoadSkill` | Request[SkillCard] | `skill_name` | Agent → Skill |
| `ReflectSkills` | Command | — | Agent → Skill |
| `SkillsReflected` | Event | `new`, `updated`, `deprecated`, `names` | Skill → TUI |
| `SkillLoaded` | Event | `skill_name`, `skill_pattern` | Skill → TUI |
| `SkillReflectError` | Event | `error` | Skill → TUI |
| `ListSkills` | Request[list[dict]] | `include_deprecated` | TUI → Skill |
| `DeleteSkill` | Request[str\|None] | `target` | TUI → Skill |
| `DeprecateSkill` | Request[str\|None] | `target` | TUI → Skill |
| `GetSkillName` | Request[str] | `skill_id` | — → Skill |
| `RecordSkillUsage` | Request[None] | `skill_id`, `positive` | — → Skill |
| `MergeSkills` | Request[dict] | — | TUI → Skill |

### 记忆契约 (`memory.py`)

全部为 Request 类型（记忆模块不主动发布事件）：

| 类型 | 返回 | 用途 |
|------|------|------|
| `GetContext` | `list[dict]` | 获取会话上下文 |
| `AppendMessages` | `None` | 追加消息 |
| `ReplaceMemory` | `None` | 替换全部记忆 |
| `ClearMemory` | `None` | 清空记忆 |

### 会话契约 (`session.py`)

| 类型 | 语义 | 用途 |
|------|------|------|
| `ListSessions` | Request[list] | 列出历史会话 |
| `LoadSession` | Request[list[dict]\|None] | 加载会话数据 |
| `SessionRestored` | Event | 会话恢复通知 |

### Cron 契约 (`cron.py`)

| 类型 | 语义 | 关键字段 | 发布者 → 订阅者 |
|------|------|---------|----------------|
| `ScheduleCron` | Request[str] | `cron`, `prompt`, `recurring`, `durable` | Agent(cron tool) → Cron |
| `CancelCron` | Request[bool] | `job_id` | Agent(cron tool) → Cron |
| `ListCronJobs` | Request[list[dict]] | — | Agent(cron_jobs tool) → Cron |
| `CronTurnRequested` | Command | `trigger` | Cron → Agent |
| `CronJobEvent` | Event | `job_id`, `name`, `status`, `prompt` | Cron → TUI |

## DTO 设计

`alex/kernel/dto/` 定义跨模块数据传递对象（纯 dataclass，零业务逻辑）：

| DTO | 用途 |
|-----|------|
| `MessageDTO` | 消息序列化格式 |
| `SkillCard` | 技能摘要卡片（id, name, pattern, instruction, tags, status...） |
| `ToolSpec` | 工具规格（name, description, json_schema, provider, metadata） |
| `ToolResult` | 工具执行结果（name, output, error, run_id） |
| `ToolExecutionContext` | 工具执行上下文（session_id, turn_id, source, metadata） |

## 具体实现

`alex/bus/in_memory.py` 提供 `AsyncEventBus` — `MessageBus` Protocol 的具体实现：

- **Event plane**：维护 `dict[type, list[EventHandler]]`，`publish()` 时创建 task 并发执行所有 handler
- **Request plane**：维护 `dict[type, ReqHandler]`，`request()` 生成 `correlation_id`，通过内部 `asyncio.Future` 等待 handler 返回
- 同一 `session_id` 的事件串行处理，不同 `session_id` 可并行
- handler 异常隔离，不拖垮总线

## 与旧通知模型的区别

| 特性 | 旧 (`bus/events.py` 裸 dataclass) | 新 (`kernel/contracts/` + `kernel/bus.py`) |
|------|----------------------------------|-------------------------------------------|
| 消息语义 | 全部 Event，语义模糊 | Event / Command / Request 三种明确语义 |
| 类型安全 | dataclass 字段类型 | Request[T] 泛型返回类型 |
| 请求-响应 | 无（需手动实现） | `request()` + `provide()` 原生支持 |
| 模块隔离 | events 散落在 `bus/events.py` | 按领域分 contract 文件 |
| 跨模块类型 | 模块间直接 import | 全部通过 kernel，模块间零导入 |
