# 类型化事件系统 (`alex/bus/events.py`)

## 设计思路

`alex/bus/events.py` 定义三层事件继承体系（`Event -> Command / DomainEvent / UIEvent`），替代早期裸 `dict` 通知模型。Cron 调度器、技能反思、Turn 编排等子系统发出具体类型的事件，TUI 或其他前端按类型分发渲染。

## 事件层次

```
Event (event_id, session_id, turn_id, source, ts)
├── Command          # 行为请求（request）
│   ├── UserTurnRequested
│   ├── CronTurnRequested
│   ├── ResumeSessionRequested
│   ├── FeedbackSubmitted
│   └── ClearSessionRequested
├── DomainEvent      # 状态变更（fact）
│   ├── TurnStarted / TurnCompleted / TurnFailed
│   ├── SkillMatched / SkillReflected
│   ├── ToolExecutionRequested / ToolExecutionCompleted
│   ├── CronScheduled / CronTriggered
│   └── CronRecordPersist
└── UIEvent          # 前端信号
    ├── ThinkingUpdated / TokenEmitted
    ├── ToolStarted / ToolFinished
    ├── SkillLoaded
    ├── SkillReflectEvent / SkillReflectErrorEvent
    ├── CronJobEvent / CronDebugEvent
    ├── CronBatch / CronDone / CronError
    ├── ToastRequested
    └── SessionRestored
```

## 常用事件

| 事件类 | 触发时机 | 关键字段 |
|--------|---------|---------|
| `UserTurnRequested` | 用户发送消息时 | `user_text` |
| `TurnStarted` | 每个 turn 开始时 | `kind` ("user"/"cron") |
| `TurnCompleted` | 每个 turn 完成时 | `messages`, `content`, `thinking` |
| `TurnFailed` | turn 异常时 | `error` |
| `ThinkingUpdated` | LLM 产出推理内容 | `delta`, `stream_id` |
| `TokenEmitted` | LLM 产出回复 token | `delta`, `stream_id` |
| `ToolStarted` | 工具调用开始 | `tool_name`, `tool_input`, `is_cron`, `stream_id` |
| `ToolFinished` | 工具调用完成 | `output`, `is_cron`, `stream_id` |
| `SkillLoaded` | Agent 加载技能详情 | `skill_name`, `skill_pattern` |
| `SkillReflectEvent` | 技能反思完成后 | `new`, `updated`, `deprecated`, `names` |
| `SkillReflectErrorEvent` | 技能反思失败时 | `error` |
| `CronJobEvent` | Cron 任务状态变更 | `job_id`, `name`, `status`, `subscribe`, `result`, `error` |
| `CronBatch` | cron turn 消息批次 | `stream_id`, `messages` |
| `CronDone` | cron turn 流完成 | `stream_id`, `content`, `thinking` |
| `CronError` | cron turn 流出错 | `stream_id`, `error` |
| `CronRecordPersist` | 跨 session cron 记录持久化 | `session_id`, `record` |
| `CronDebugEvent` | Cron 调试日志 | `message` |

## 分发机制

所有事件通过 `AsyncEventBus` 分发：

- 发布：`bus.publish(event)`
- 订阅：`await bus.subscribe(EventType, handler)`
- 同一 `session_id` 的事件串行处理，不同 `session_id` 可并行
- handler 异常隔离，不拖垮总线

## 使用方式

### 用户 turn

```python
# Agent.chat_stream() 发布
self.push_notification(UserTurnRequested(session_id=sid, user_text=msg))

# TurnOrchestrator.run() 发布
self._push_notification(TurnStarted(session_id=sid, turn_id=tid, ...))
self._push_notification(TurnCompleted(session_id=sid, turn_id=tid, ...))
# 异常时发布 TurnFailed
self._push_notification(TurnFailed(session_id=sid, turn_id=tid, ...))
```

### Cron 链路

```python
# CronManager 运行时 emit
self._emit(CronJobEvent(job_id=..., name=..., status=..., subscribe=..., ...))

# CronTurnHandler 流式事件
push_notification(ThinkingUpdated(stream_id=sid, delta=...))
push_notification(TokenEmitted(stream_id=sid, delta=...))
push_notification(CronBatch(stream_id=sid, messages=...))
push_notification(CronDone(stream_id=sid, content=..., thinking=...))
```

### TUI 消费

TUI 通过总线订阅 handler 处理事件：

- `UserTurnRequested` → 仅可观测性（暂不驱动渲染）
- `CronJobEvent` → 状态栏刷新 + toast + 持久化
- `ToolStarted(is_cron=True)` → StreamRenderer 创建 cron bubble
- `TokenEmitted(stream_id)` → StreamRenderer 追加响应文本
- `CronDone(stream_id)` → StreamRenderer 完成气泡 + ChatHistory.add()
- `SkillReflectEvent` → SystemBubble + Toast
- `TurnCompleted` → SessionPersistence 自动保存

## 与旧通知模型的区别

| 特性 | 旧 (`events.py` 裸 dict) | 新 (`bus/events.py`) |
|------|--------------------------|----------------------|
| 类型安全 | dict 键拼写风险 | dataclass 字段类型 |
| 分发 | Agent `_pending_notifications` 轮询 | AsyncEventBus push |
| 订阅 | 无（TUI 轮询消费全部） | 按类型订阅，handler 隔离 |
| 跨线程 | 手动 append 到列表 | `loop.call_soon_threadsafe` |
