# 流式输出

## 设计思路

流式输出基于 `ChatClient.stream_chat()`（OpenAI SDK 的 async stream），`TurnProcessor` 统一处理用户 turn 与 cron turn，并通过 bus 发布类型化的 UI 事件（`ThinkingUpdated`、`TokenEmitted`、`ToolStarted`、`ToolFinished`、`SkillLoaded`）。这些事件类型定义在 `alex/kernel/contracts/` 中。

> **注意**：早期 `alex/streaming/` 模块和 LangGraph `astream_events` 已在架构重构中移除。流式事件现在通过 `AsyncEventBus` 分发，事件类型纳入 `kernel/contracts/` 的 `Event` 体系。

## 事件类型

| 事件 | 合约文件 | 触发时机 |
|------|---------|---------|
| `ThinkingUpdated` | `chat.py` | LLM 产出推理/思考内容（DeepSeek reasoning_content） |
| `TokenEmitted` | `chat.py` | LLM 产出正式回复 token |
| `ToolStarted` | `tools.py` | 开始调用工具 |
| `ToolFinished` | `tools.py` | 工具调用完成 |
| `SkillLoaded` | `skills.py` | Agent 通过 load_skill 工具加载技能详情 |

## 用户 turn 流式路径

```
TurnProcessor.stream_user_turn()
  └── ChatClient.stream_chat(messages, tools)
        ├── on delta → bus.publish(TokenEmitted)
        ├── on reasoning → bus.publish(ThinkingUpdated)
        ├── on tool_call → TurnProcessor 解析
        │   ├── load_skill? → bus.publish(SkillLoaded)
        │   └── 其他工具 → bus.request(ExecuteTool)
        │       ├── bus.publish(ToolStarted) ← ToolsModule
        │       └── bus.publish(ToolFinished) ← ToolsModule
        └── on end → bus.publish(TurnCompleted)
```

事件通过 `AsyncEventBus` 从 Agent 传递到 TUI。`TuiModule._route_to_app()` 将所有订阅事件转发到 `AlexApp.post_message()`，由 `StreamRenderer` 统一处理渲染。

## Cron turn 流式路径

```
TurnProcessor.run_cron_turn()
  └── ChatClient.stream_chat(messages, tools)
        ├── on delta → bus.publish(TokenEmitted(stream_id=...))
        ├── on reasoning → bus.publish(ThinkingUpdated(stream_id=...))
        ├── on tool_call → 同上工具执行路径
        └── on end → 收集结果 → return
```

事件同样通过 `AsyncEventBus` 分发到 TUI 的订阅者。

## 渲染统一

用户 turn 和 cron turn 的流式渲染逻辑由 `alex/tui/stream_renderer.py` 中的 `StreamRenderer` 类统一管理：

- `on_thinking(delta)` — 累积 thinking
- `on_token(delta)` — 累积文本 + 更新 bubble
- `on_tool_started(tool_id, name, args)` — 创建 inflight 工具 + ToolBubble
- `on_tool_finished(tool_id, output)` — 标记工具完成
- `on_skill_loaded(name, pattern)` — 记录技能
- `build_turn(user_input, kind)` — 构建 ChatTurn
- `finalize(turn)` — 完成 bubble 渲染

用户 turn 在此基础上增加 ~50ms UI 节流以保持流畅。

## DeepSeek thinking 支持

- `reasoning_content` 通过 OpenAI SDK stream chunk 的 `additional_kwargs` 或专用属性提取
- 作为 `ThinkingUpdated` 事件 publish
- 反思在流结束后通过 `bus.publish(ReflectSkills)` 异步执行，不阻塞流式输出
