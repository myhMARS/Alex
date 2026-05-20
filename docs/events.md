# 类型化事件系统 (`alex/events.py`)

## 设计思路

`alex/events.py` 定义类型化的事件数据类（dataclass），替代裸 `dict` 在 Agent 内部各层之间传递通知。Cron 调度器、技能反思等子系统发出具体类型的事件，TUI 或其他前端按类型分发渲染。

## 事件类型

| 事件类 | 触发时机 | 字段 |
|--------|---------|------|
| `SkillReflectEvent` | 技能反思完成后 | `new: int`, `updated: int`, `deprecated: int`, `names: list[str]` |
| `SkillReflectErrorEvent` | 技能反思失败时 | `error: str` |
| `CronJobEvent` | Cron 任务完成一次运行或状态变更 | `job_id`, `name`, `status`, `subscribe`, `result`, `error`, `tool_call_id` |
| `CronDebugEvent` | Cron 调试日志（需 `ALEX_CRON_DEBUG=1` 开启） | `message: str` |

## 使用方式

### CronManager 内部

`CronManager` 在其事件循环线程中创建事件实例，通过 `_emit()` 方法桥接到 Agent 的通知回调：

```python
# cron.py 内部
self._emit(CronJobEvent(
    job_id=job.id,
    name=job.name,
    status=run_status,
    subscribe=job.subscribe,
    result=job.last_result or "",
    error=job.last_error or "",
    tool_call_id=f"cron:{job.id}:{run_seq}",
))
```

### Agent 通知队列

Agent 的 `push_notification()` 接收事件实例，对于 `CronJobEvent` 且 `subscribe=True` 的完成事件，自动触发 `_stream_cron_reply()` 流式回复流程。

```python
def push_notification(self, note: dict) -> None:
    self._pending_notifications.append(note)
    if note.get("type") == "cron_job_done":
        # 检查 subscribe 标志，启动流式回复
        ...
```

### TUI 消费

TUI 通过 `pop_notifications()` 拉取通知，按事件类型分发到不同的 UI 更新路径：

- `SkillReflectEvent` → `SystemBubble` 消息 + Toast
- `CronJobEvent` → 状态栏刷新 + 订阅流渲染
- `CronDebugEvent` → Toast（调试模式）

## 与 StreamEvent 的关系

| 类型 | 用途 | 位置 |
|------|------|------|
| `events.py` 事件类 | Agent 内部子系统间通知（Cron → Agent，Skills → Agent） | `alex/events.py` |
| `StreamEvent` | LLM 流式输出的逐 token / tool call 事件 | `alex/streaming/handler.py` |

两者互不依赖，各司其职。Cron 订阅流同时使用两类事件：`CronJobEvent` 触发流启动，流内使用 `StreamEvent` 类型渲染。
