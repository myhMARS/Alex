# Agent 核心编排层 (`alex/agent/`)

## 职责

Agent 是整个系统的薄编排 facade，聚合以下子组件协调 LLM、Memory、Tools、Skills、Cron、Session 各层：

| 子组件 | 文件 | 职责 |
|--------|------|------|
| `SessionService` | `session_service.py` | Session 持久化边界 + 历史恢复（拥有 `deserialize_message`） |
| `CronService` | `cron_service.py` | CronManager 生命周期封装 |
| `TurnOrchestrator` | `orchestrator.py` | 用户 turn 编排（LLM 流 + 工具调用 + 记忆写入） |
| `CronTurnHandler` | `cron_handler.py` | Cron 触发后的流式 LLM 回复 |
| `FeedbackRecorder` | `feedback.py` | 技能反馈记录与条件反思触发 |
| `PromptAssembler` | `prompt.py` | 动态 prompt 组装（技能目录注入） |

Agent 自身不直接依赖 `CronManager`、`SessionPersistence` 或 `deserialize_message`——这些边界已通过 `CronService` 和 `SessionService` 收口。

## 对外接口 (AgentFacade Protocol)

| 方法 | 说明 |
|------|------|
| `chat_stream(message) → AsyncIterator` | 流式对话，yield ThinkingUpdated / TokenEmitted / ToolStarted / ToolFinished / SkillLoaded |
| `register_tool(tool)` / `unregister_tool(name)` | 动态工具管理，重建 LangGraph |
| `clear_history()` | 清空对话记忆 |
| `restore_history(messages)` | 从消息列表恢复对话历史（委托 SessionService） |
| `history` (property) | 同步获取当前对话历史 |
| `provide_feedback(positive, turn_id)` | 用户反馈（驱动技能进化 & 负反馈触发反思） |
| `is_reflecting` (property) | 是否正在执行反思 |
| `bind_event_loop(loop)` | 绑定事件循环（委托 CronService） |
| `start_services()` | 启动后台服务（委托 CronService） |
| `shutdown()` | 关闭后台服务（委托 CronService） |
| `list_cron_jobs()` / `cancel_cron_job(job_id)` | cron 任务管理（委托 CronService） |
| `schedule_cron_job(**kwargs)` | 创建定时任务（委托 CronService） |
| `list_session_cron_history(query, limit)` | 当前 session 的 cron 执行历史 |
| `execute_tool_action(session_id, action, params)` | 按名称执行工具（cron runner 入口） |
| `reflect() → dict` | 强制触发技能反思 |
| `list_skills()` / `delete_skill(target)` / `deprecate_skill(target)` | 技能 CRUD |
| `merge_skills() → dict` | LLM 驱动的技能去重合并 |
| `list_sessions()` / `load_session(id)` / `subscribe_store(bus)` | 会话持久化（委托 SessionService） |

## 事件发布

Agent 通过 `push_notification()` 向 EventBus 发布以下事件：

| 事件 | 触发时机 |
|------|---------|
| `UserTurnRequested` | 每个用户 turn 开始时（提升可观测性） |
| `CronJobEvent` | Cron 任务状态变更 |
| `SkillReflectEvent` / `SkillReflectErrorEvent` | 技能反思完成/失败 |

`TurnStarted`、`TurnCompleted`、`TurnFailed` 由 `TurnOrchestrator` 和 `CronTurnHandler` 直接发布。

## 内置工具

Agent 自动注册两个内置工具：

- `load_skill` — Agent 按需加载技能的完整执行流程
- `cron_history` — 查询当前 session 的 cron 执行历史

## Cron 后台任务

- 基于 **APScheduler** 的异步调度器（通过 `CronService -> CronManager`）
- 支持 `interval_seconds` 和 5-6 字段 **crontab** 两种触发方式
- `subscribe=true` 时，每次执行结果以流式对话形式注入 TUI
- Agent 的 `execute_tool_action()` 作为 runner 注入 CronService

## 并发模型

`_turn_lock`（`asyncio.Lock`）确保同一时间只有一个对话轮次在操作 Memory：

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

## 完整对话流程

```
User Input
  │
  ├─► [Bus] bus.publish(UserTurnRequested)
  │
  ├─► [Skills] PromptAssembler.ensure_skills_prompt(query) → 技能目录注入
  │
  ├─► [Memory] Memory.get_context(session_id) → 获取历史上下文
  │
  ├─► [LLM/Graph] 执行 LangGraph Agent
  │       ├─► LLM 推理（thinking + content）
  │       │     └─► yield ThinkingUpdated / TokenEmitted
  │       ├─► [Tools] 工具调用（如需要）
  │       │     └─► load_skill → 按需加载技能详细流程
  │       │     └─► yield ToolStarted / ToolFinished / SkillLoaded
  │       └─► 循环至最终回复
  │
  ├─► [Memory] 更新记忆（含 reasoning_content）
  │
  ├─► [Bus] bus.publish(TurnCompleted) → SessionPersistence 自动保存
  │
  ├─► [Skills] 记录技能使用情况 & episode
  │
  └─► [Skills] maybe_reflect() — 条件触发反思
```

## 反思机制

- 每 5 轮对话自动触发
- 新领域（无技能匹配）时触发
- 用户负反馈（Ctrl+B）时异步触发
- `/reflect` 命令手动触发
- 反思结果通过 `SkillReflectEvent` 推送到 EventBus

## 目录结构

```
alex/agent/
├── __init__.py
├── service.py          # Agent facade（薄编排层）
├── session_service.py  # Session 持久化 + 历史恢复边界
├── cron_service.py     # CronManager 生命周期封装
├── orchestrator.py     # TurnOrchestrator — 用户 turn 编排
├── cron_handler.py     # CronTurnHandler — cron 流式回复
├── feedback.py         # FeedbackRecorder — 技能反馈与反思
├── prompt.py           # PromptAssembler — 动态 prompt 组装
└── ports.py            # AgentFacade Protocol
```
