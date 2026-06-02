# Agent 对话引擎 (`alex/agent/`)

## 职责

`AgentModule` 是对话引擎的 bus 入口，订阅 `UserTurnRequested` 和 `CronTurnRequested`，驱动完整对话循环。模块内的 `Agent` 类仅依赖 `AsyncEventBus`，通过 kernel 契约与 Memory、Tools、Skill 等模块交互。

**核心设计原则**：Agent 不直接导入其他模块的内部实现，所有跨模块能力调用走 `bus.request(RequestType(...))`。

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `AgentModule` | `module.py` | bus 入口：订阅 UserTurnRequested / CronTurnRequested，实现 TurnServices 协议 |
| `Agent` | `service.py` | 薄 facade：管理 ChatAppService，提供 chat_stream / execute_cron_prompt |
| `ChatAppService` | `chat_service.py` | 核心引擎：LLM 循环、工具执行、turn 管理 |
| `TurnProcessor` | `turn_processor.py` | 单消费者 FIFO：统一处理 user turn 与 cron turn 的流式执行、记忆写入和事件发布 |

## 模块启动

```
AgentModule.start(bus)
  ├─ 创建 Agent(bus, system_prompt)
  ├─ agent.start_services()  → 创建 ChatClient + start bus
  ├─ bus.subscribe(UserTurnRequested, _on_user_turn)
  └─ bus.subscribe(CronTurnRequested, _on_cron_turn)
```

## 跨模块通信 (TurnServices)

`AgentModule` 实现 TurnServices 协议，将所有跨模块调用委托给 bus request：

| 方法 | Bus Request | 返回 |
|------|-------------|------|
| `get_memory_context(session_id)` | `GetContext` | `list[dict]` |
| `append_memory(session_id, messages)` | `AppendMessages` | `None` |
| `get_skill_by_name(skill_name)` | `LoadSkill` | `SkillCard \| None` |
| `retrieve_skills(query, top_k)` | `RetrieveSkills` | `list[SkillCard]` |
| `get_tool_catalog()` | `GetToolCatalog` | `list[ToolSpec]` |
| `execute_tool(ctx, name, args)` | `ExecuteTool` | `ToolResult` |

## 对外接口

`Agent` 类暴露的公开方法（供 `AgentModule` 和 `AlexApp` 调用）：

| 方法 | 说明 |
|------|------|
| **生命周期** | |
| `start_services()` | 创建 ChatClient（如果未注入），启动 bus |
| `shutdown()` | 关闭 ChatAppService |
| **事件总线** | |
| `bus` (property) | 当前 EventBus 实例 |
| `bind_event_bus(bus)` | 绑定事件总线 |
| **会话上下文** | |
| `set_session_context(session_id, cron_history)` | 设置当前 session 上下文 |
| `session_id` (property) | 当前 session ID |
| **对话** | |
| `chat_stream(message)` | 执行用户 turn，事件通过 bus 广播 |
| `last_turn_result` (property) | 最后一轮的流式执行结果 |
| `execute_cron_prompt(...)` | 执行 cron turn prompt |

## 事件发布

对话过程中通过 bus 发布以下事件（定义在 `alex/kernel/contracts/`）：

| 事件 | 类型 | 触发时机 |
|------|------|---------|
| `UserTurnRequested` | Command | TUI 发布，Agent 订阅 |
| `CronTurnRequested` | Command | CronModule 发布，Agent 订阅 |
| `TurnStarted` | Event | 每个 turn 开始时 |
| `TurnCompleted` | Event | 每个 turn 完成时（StoreModule 订阅 → 持久化） |
| `TurnFailed` | Event | turn 异常时 |
| `ThinkingUpdated` | Event | LLM 产出推理内容 |
| `TokenEmitted` | Event | LLM 产出回复 token |
| `ToolStarted` | Event | 工具调用开始 |
| `ToolFinished` | Event | 工具调用完成 |

## 用户 Turn 流程

```
TUI 用户输入
  │
  ▼
TuiModule.publish_user_turn() → bus.publish(UserTurnRequested)
  │
  ▼
AgentModule._on_user_turn()
  ├─ asyncio.create_task(_process_user_turn)  ← 不阻塞 bus dispatch loop
  │
  ├─ Agent.set_session_context(session_id)
  ├─ Agent.chat_stream(user_text)
  │   ├─ bus.publish(TurnStarted)
  │   ├─ TurnProcessor.stream_user_turn()
  │   │   ├─ bus.request(GetContext) → MemoryModule 返回历史
  │   │   ├─ bus.request(RetrieveSkills) → SkillModule 检索技能
  │   │   ├─ bus.request(GetToolCatalog) → ToolsModule 返回工具目录
  │   │   ├─ ChatClient.stream_chat() → yield tokens
  │   │   │   ├─ bus.publish(ThinkingUpdated) / bus.publish(TokenEmitted)
  │   │   │   └─ 工具调用时：
  │   │   │       ├─ bus.request(ExecuteTool) → ToolsModule 执行 + 权限检查
  │   │   │       └─ bus.publish(ToolStarted) / bus.publish(ToolFinished)
  │   │   └─ bus.publish(TurnCompleted) → StoreModule 持久化
  │   └─ return
  │
  └─ 如果本轮未匹配 skill → bus.publish(ReflectSkills)
```

## Cron Turn 流程

```
CronManager fire
  │
  ▼
CronModule._cron_runner() → bus.publish(CronTurnRequested)
  │
  ▼
AgentModule._on_cron_turn()
  ├─ asyncio.create_task(_process_cron_turn)
  └─ Agent.execute_cron_prompt(session_id, job_id, name, prompt, stream_id)
      └─ ChatAppService.execute_cron_prompt()
          └─ TurnProcessor.run_cron_turn() → 流式事件通过 bus 广播
```

## 并发模型

`TurnProcessor` 使用单消费者 FIFO 队列，确保同一时间只有一个对话轮次在操作 Memory：

```
User / cron turn submit
     │
     ├─ enqueue FIFO
     │
     └─ TurnProcessor single consumer
          ├─ read memory (bus.request GetContext)
          ├─ stream LLM (ChatClient)
          ├─ write batch (bus.request AppendMessages)
          └─ process next queued turn
```

权限 confirm modal 也通过 `NotificationController` 内部的 `_confirm_lock` 串行化，避免 user turn 与 cron turn 同时弹窗。

## 目录结构

```
alex/agent/
├── __init__.py
├── module.py                   # AgentModule — bus 入口，订阅 UserTurnRequested
├── service.py                  # Agent — 薄 facade，仅依赖 bus
├── chat_service.py             # ChatAppService — LLM 循环 + 工具执行
└── turn_processor.py           # TurnProcessor — 统一 user/cron turn FIFO
```
