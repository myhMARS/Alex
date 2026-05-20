# Agent 核心编排层 (`alex/agent.py`)

## 职责

Agent 是整个系统的编排中心，协调 LLM、Memory、Tools、Skills、Cron、Streaming 各层。

## 对外接口

| 方法 | 说明 |
|------|------|
| `chat_stream(message) → AsyncIterator[StreamEvent]` | 流式对话，yield thinking/token/tool_start/tool_end/skill_load/done 事件 |
| `register_tool(tool)` / `unregister_tool(name)` | 动态工具管理，重建 LangGraph |
| `clear_history()` | 清空对话记忆 |
| `restore_history(turns)` | 从 ChatTurn 列表恢复对话历史（含 tool call 链） |
| `history` (property) | 同步获取当前对话历史 |
| `provide_feedback(positive: bool)` | 用户反馈（驱动技能进化 & 负反馈触发反思） |
| `is_reflecting` (property) | 是否正在执行反思 |
| `bind_event_loop(loop)` | 绑定事件循环（TUI 模式必需，用于 Cron 线程安全通知） |
| `start_services()` | 启动后台服务（APScheduler） |
| `shutdown()` | 关闭后台服务，释放资源 |
| `list_cron_jobs() → list[dict]` | 列出所有后台定时任务 |
| `cancel_cron_job(job_id) → bool` | 取消指定定时任务 |
| `pop_notifications() → list[dict]` | 拉取并清空待处理通知队列 |
| `push_notification(note)` | 推送系统通知到 UI 层 |
| `reflect() → dict` | 强制触发技能反思 |
| `list_skills() → list[dict]` | 列出所有技能及元数据 |
| `delete_skill(target) → str | None` | 按名称或 ID 前缀删除技能 |
| `deprecate_skill(target) → str | None` | 按名称或 ID 前缀废弃技能 |
| `merge_skills() → dict` | LLM 驱动的技能去重合并 |

## 流式事件类型

| 事件 | 说明 |
|------|------|
| `thinking` | LLM 推理/思考内容（DeepSeek reasoning_content） |
| `token` | 正式回复 token |
| `tool_start` | 工具调用开始 |
| `tool_end` | 工具调用完成 |
| `skill_load` | Agent 通过 load_skill 工具加载技能详情 |
| `done` | 本轮对话结束 |

## 内置工具

Agent 自动注册 `load_skill` 工具，允许 Agent 按需加载技能的完整执行流程。

## 通知系统

Agent 维护 `_pending_notifications` 队列，TUI 通过 `pop_notifications()` 轮询消费。内部使用 `alex/events.py` 中类型化的 dataclass 事件（`SkillReflectEvent`、`CronJobEvent` 等）替代裸 dict 分发。通知类型：

| 类型 | 说明 |
|------|------|
| `skill_reflect` | 反思完成（新增/更新/废弃技能数） |
| `skill_reflect_error` | 反思失败 |
| `cron_debug` | Cron 调试消息 |
| `cron_job_update` | 定时任务状态变更 |
| `cron_job_done` | 定时任务执行完成 |
| `cron_stream_start` | Cron 订阅流开始（创建新 bubble） |
| `cron_stream_token` | Cron 订阅流 token 增量 |
| `cron_stream_thinking` | Cron 订阅流 thinking 增量 |
| `cron_stream_tool_start` | Cron 订阅流工具调用开始 |
| `cron_stream_tool_end` | Cron 订阅流工具调用完成 |
| `cron_stream_done` | Cron 订阅流完成（bubble finalize） |
| `cron_stream_error` | Cron 订阅流出错 |

## Cron 后台任务

- 基于 **APScheduler** 的异步调度器
- 支持 `interval_seconds` 和 5-6 字段 **crontab** 两种触发方式
- `subscribe=true` 时，每次执行结果以流式对话形式注入 TUI
- Agent 重新构建 graph 注入 cron 结果作为 ToolMessage，生成自然语言回复
- **`_turn_lock`**（`asyncio.Lock`）序列化整个对话轮次（read → stream → write），cron 流回复自动等待用户对话结束后执行

## 并发模型

`_turn_lock` 确保同一时间只有一个对话轮次在操作 Memory：

```
User chat (holds lock)        Cron reply (waits)
     │                            │
     ├─ read memory               │  ← await lock
     ├─ stream LLM                │
     ├─ write batch               │
     └─ release lock ─────────────┤
                                  ├─ read memory (latest state)
                                  ├─ stream LLM
                                  ├─ write batch
                                  └─ release lock
```

BufferMemory 内部的 `_write_lock` 作为第二层防护，确保批量写入原子化。

## 完整对话流程

```
User Input
  │
  ├─► [Skills] SkillManager.inject_skills_prompt(query) → 技能目录注入
  │
  ├─► [Memory] Memory.get_context(query) → 获取历史上下文
  │
  ├─► 构建增强 prompt = system_prompt + 技能提示
  │
  ├─► [LLM/Graph] 执行 LangGraph Agent
  │       ├─► LLM 推理（thinking + content）
  │       ├─► [Tools] 工具调用（如需要）
  │       │     └─► load_skill → 按需加载技能详细流程
  │       └─► 循环至最终回复
  │
  ├─► [Memory] 更新记忆（含 reasoning_content）
  │
  ├─► [Skills] 记录技能使用情况 & episode
  │
  ├─► [Skills] _maybe_reflect() — 条件触发反思
  │
  └─► [Notifications] 推送 skill_reflect 等通知到 UI
```

## 反思机制

- 每 5 轮对话自动触发
- 新领域（无技能匹配）时触发
- 用户负反馈（Ctrl+B）时异步触发
- `/reflect` 命令手动触发
- 反思结果通过 `_pending_notifications` 推送到 TUI
