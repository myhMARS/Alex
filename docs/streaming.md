# 流式输出

## 设计思路

基于 LangGraph 的 `astream_events` API，`TurnProcessor` 统一处理用户 turn 与 cron turn，并对外发送类型化的 UI 事件（`ThinkingUpdated`、`TokenEmitted`、`ToolStarted`、`ToolFinished`、`SkillLoaded`）。这些事件类定义在 `alex/bus/events.py` 中。

> **注意**：早期 `alex/streaming/` 模块（`StreamEvent` + `StreamHandler`）已在事件总线重构中移除。流式事件类型现在统一纳入 `Event -> UIEvent` 继承体系。

## 事件类型

| 事件 | 触发时机 |
|------|---------|
| `ThinkingUpdated` | LLM 产出推理/思考内容（DeepSeek reasoning_content） |
| `TokenEmitted` | LLM 产出正式回复 token |
| `ToolStarted` | 开始调用工具 |
| `ToolFinished` | 工具调用完成 |
| `SkillLoaded` | Agent 通过 load_skill 工具加载技能详情 |

## 用户 turn 流式路径

```
TurnProcessor.stream_user_turn()
  └── graph.astream_events()
        ├── on_chat_model_stream → yield ThinkingUpdated / TokenEmitted
        ├── on_tool_start       → yield SkillLoaded + ToolStarted
        └── on_tool_end         → yield ToolFinished
```

事件通过 async generator 直接从 `Agent.chat_stream()` 传递到 TUI 的 `_run_chat()` 循环。

## Cron turn 流式路径

```
TurnProcessor.run_cron_turn()
  └── graph.astream_events()
        ├── on_chat_model_stream → bus.publish(ThinkingUpdated(stream_id=...))
        ├── on_tool_start       → bus.publish(ToolStarted(is_cron=True, stream_id=...))
        └── on_tool_end         → bus.publish(ToolFinished(is_cron=True, stream_id=...))
```

事件通过 `AsyncEventBus` 分发到 TUI 的 cron 订阅者，由 `StreamRenderer` 统一处理。

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

- `reasoning_content` 通过 `chunk.additional_kwargs` 提取
- 作为 `ThinkingUpdated` 事件 yield/publish
- 反思在流结束后通过 `maybe_reflect()` 异步执行，不阻塞流式输出
