# 流式输出 (`alex/streaming/`)

## 设计思路

基于 LangGraph 的 `astream_events` API，将流式事件抽象为统一的 `StreamEvent`，通过 `StreamHandler` 分发给监听器。Agent 的 `chat_stream()` 方法直接 yield 这些事件，TUI 层消费并实时渲染。

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
2. DeepSeek 的 `reasoning_content` 通过 `additional_kwargs` 提取，作为 `thinking` 事件 yield
3. `StreamHandler` 支持注册多个 listener（TUI 更新、日志记录等）
4. 反思在流结束后异步执行（`asyncio.ensure_future`），不阻塞 `done` 事件

## 目录结构

```
alex/streaming/
├── __init__.py
└── handler.py        # StreamEvent 数据类 + StreamHandler
```
