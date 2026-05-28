# Agent 核心编排层 (`alex/agent/`)

## 职责

Agent 是整个系统的薄编排 facade，把 LLM、Memory、Tools（含权限策略）、Skills、Cron、Session 各层组合起来，并通过 `AsyncEventBus` 与 TUI / 持久化层交互。

业务逻辑分散在 5 个独立的 application service 中，`Agent` 自己只负责装配 + 代理调用：

| 子组件 | 文件 | 职责 |
|--------|------|------|
| `ChatAppService` | `chat_service.py` | 聊天流、工具执行、LangGraph 管理；持有 `ToolRegistry` / `ToolExecutor` / `PermissionPolicy` |
| `SessionService` | `session_service.py` | session 持久化边界 + 历史恢复（通过 `store/session_serializer`） |
| `CronService` | `cron_service.py` | `CronManager` 生命周期封装（绑定 loop / start / shutdown / schedule / cancel） |
| `FeedbackAppService` | `feedback_service.py` | 用户评分、episodes 采集、条件反思触发；per-session state 隔离 |
| `SkillAdminAppService` | `skill_admin_service.py` | 技能 CRUD / merge / load_skill 入口 |
| `TurnOrchestrator` | `orchestrator.py` | 单个用户 turn 编排（LLM 流 + 工具调用 + 记忆写入 + 事件发布） |
| `CronTurnHandler` | `cron_handler.py` | cron 触发后的流式 LLM 回复 |
| `PromptAssembler` | `prompt.py` | 动态 prompt 组装（技能目录注入） |

`create_agent()`（`factory.py`）作为对外的装配入口：默认值合并、权限策略构造、`AuditLogger` 挂载、用户插件加载，全在这一处完成。`main.py` 通过它取得 ready-to-use 的 `Agent` 实例。

Agent 自身**不**直接依赖 `CronManager`、`SessionPersistence`、`deserialize_message` 等内部实现 —— 这些边界已通过对应的 service 收口。

## 对外接口 (AgentFacade Protocol)

| 方法 | 说明 |
|------|------|
| `chat_stream(message) → AsyncIterator` | 流式对话，yield ThinkingUpdated / TokenEmitted / ToolStarted / ToolFinished / SkillLoaded |
| `register_tool(tool)` / `unregister_tool(name)` | 动态工具管理；注册时自动按权限元数据 gate，重建 LangGraph |
| `permissions` (property) / `set_permissions(policy)` | 读取或替换权限策略（运行时可注入 TUI 的 confirm hook） |
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
| `execute_tool_action(session_id, action, params)` | 按名称执行工具（cron runner 入口；构造 `ToolExecutionContext`，受权限策略约束） |
| `reflect() → dict` | 强制触发技能反思 |
| `list_skills()` / `delete_skill(target)` / `deprecate_skill(target)` | 技能 CRUD |
| `merge_skills() → dict` | LLM 驱动的技能去重合并 |
| `list_sessions()` / `load_session(id)` / `subscribe_store(bus)` | 会话持久化（委托 SessionService） |

## 事件发布

Agent 通过 `push_notification()` 向 EventBus 发布以下事件：

| 事件 | 触发时机 |
|------|---------|
| `UserTurnRequested` | 每个用户 turn 开始时（提升可观测性） |
| `CronJobEvent` | Cron 任务状态变更（含 `subscribe=True` 时 cron handler 自动接力） |
| `SkillReflectEvent` / `SkillReflectErrorEvent` | 技能反思完成/失败 |

`TurnStarted` / `TurnCompleted` / `TurnFailed` 由 `TurnOrchestrator` 和 `CronTurnHandler` 直接发布。

## 内置工具

Agent 自动注册两个内置工具（无需主程序显式装配）：

- `load_skill` — 按名加载技能完整执行流程，命中后 `SkillLoaded` 事件推到 TUI
- `cron_history` — 查询当前 session 的 cron 执行历史

`main.py` / `create_agent()` 还会再注册一组本地能力工具：`read` / `write` / `edit` / `grep` / `glob` / `git_inspect` / `bash` / `pwsh` / `time` / `web_search` / `web_fetch` / `cron`。详见 [tools.md](./tools.md)。

## Cron 后台任务

- 基于 **APScheduler** 的异步调度器（通过 `CronService → CronManager`）
- 支持 `interval_seconds` 和 5-6 字段 **crontab** 两种触发方式
- `subscribe=true` 时，每次执行结果以流式对话形式注入 TUI（cron turn 走与用户 turn 共享的 `StreamRenderer`）
- Agent 的 `execute_tool_action()` 作为 runner 注入 CronService；调用同样受 `PermissionPolicy` 与审计日志约束

## 权限与审计

`ChatAppService` 在构造时持有一个 `PermissionPolicy`：

- 启动时由 `create_agent()` 通过 `PermissionPolicy.from_env(audit_logger=AuditLogger(...))` 创建
- 默认放开 `read` + `network`；`write` / `shell` 必须显式授权或走 confirm hook
- `register_tool(...)` 注册路径会自动给带 `metadata["required_permission"]` 的工具包一层权限 gate
- LangGraph 调用 `tool.ainvoke()` 时直接命中 gate；`ToolExecutor`（cron runner 路径）也会查同一个策略，但已 gate 的工具由 wrapper 自检以避免双重 prompt
- `set_permissions(policy)` 在 TUI mount 时被调用，注入 `NotificationController.confirm_permission` 作为 `confirm_hook`，让 modal 弹出 diff 预览
- 每次决策（`auto_allow` / `auto_deny` / `allow_once` / `allow_always` / `deny`）由 `AuditLogger` 异步追加到 `~/.alex/audit/permissions.jsonl`

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

权限 confirm modal 也通过 `NotificationController` 内部的 `_confirm_lock` 串行化，避免用户 turn 与 cron turn 同时弹窗。

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
  │       ├─► [Tools] 工具调用
  │       │     ├─► permission gate 拦截 → 必要时弹 modal
  │       │     ├─► AuditLogger 异步追加决策
  │       │     ├─► 通过 → 实际执行；否则返回 "Error: ... blocked"
  │       │     └─► load_skill / read / edit / bash / ... 等
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
- 反思结果通过 `SkillReflectEvent` 推送到 EventBus，由 TUI `NotificationController` 转成 toast

## 目录结构

```
alex/agent/
├── __init__.py
├── service.py                  # Agent facade（薄编排层）
├── factory.py                  # create_agent() — 装配 + 权限 + AuditLogger + 插件
├── chat_service.py             # ChatAppService（聊天流、工具执行、图管理、权限）
├── session_service.py          # Session 持久化 + 历史恢复边界
├── cron_service.py             # CronManager 生命周期封装
├── feedback_service.py         # FeedbackAppService — 评分 / episodes / 反思
├── skill_admin_service.py      # SkillAdminAppService — 技能 CRUD / merge
├── orchestrator.py             # TurnOrchestrator — 用户 turn 编排
├── cron_handler.py             # CronTurnHandler — cron 流式回复
├── feedback.py                 # FeedbackRecorder（兼容保留）
├── prompt.py                   # PromptAssembler — 动态 prompt 组装
└── ports.py                    # AgentFacade Protocol
```
