# 流式输出 (`alex/streaming/`)

## 设计思路

基于 LangGraph 的 `astream_events` API，将流式事件抽象为统一的 `StreamEvent`。Agent 的 `chat_stream()` 方法直接 yield `StreamEvent`，TUI 层消费并实时渲染。`StreamHandler` 提供可选的 Listener 分发机制供外部扩展使用。

## 事件类型

| 事件 | 触发时机 |
|------|---------|
| `thinking` | LLM 产出推理/思考内容（DeepSeek reasoning_content） |
| `token` | LLM 产出正式回复 token |
| `tool_start` | 开始调用工具 |
| `tool_end` | 工具调用完成 |
| `skill_load` | Agent 通过 load_skill 工具加载技能详情 |
| `done` | 整轮对话结束 |
| `error` | 发生错误 |

## 业务逻辑

1. `chat_stream()` 内部调用 `graph.astream_events()`，逐事件解析
2. DeepSeek 的 `reasoning_content` 通过 `chunk.additional_kwargs` 提取，作为 `thinking` 事件 yield
3. 反思在流结束后通过 `_maybe_reflect()` 同步执行，不阻塞 `done` 事件
4. `StreamHandler.wrap()` 可用于包装流，将事件自动分发给注册的 listener

## 目录结构

```
alex/streaming/
├── __init__.py
└── handler.py        # StreamEvent 数据类 + StreamHandler
```
