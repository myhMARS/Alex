# Agent 核心编排层 (`alex/agent.py`)

## 职责

Agent 是整个系统的编排中心，协调 LLM、Memory、Tools、Skills、Streaming 各层。

## 对外接口

| 方法 | 说明 |
|------|------|
| `chat(message) → ChatResponse` | 非流式对话，返回 str 子类（附带 `.thinking` 属性） |
| `chat_stream(message) → AsyncIterator[StreamEvent]` | 流式对话，yield thinking/token/tool 事件 |
| `register_tool(tool)` / `unregister_tool(name)` | 动态工具管理 |
| `clear_history()` | 清空对话历史 |
| `provide_feedback(positive: bool)` | 用户反馈（驱动技能进化） |

## ChatResponse

`str` 子类，保持向后兼容的同时携带 thinking 内容：

```python
response = await agent.chat("hello")
print(response)           # 正常字符串行为
print(response.thinking)  # 访问 DeepSeek reasoning_content
```

## 流式事件类型

| 事件 | 说明 |
|------|------|
| `thinking` | LLM 推理/思考内容（DeepSeek reasoning_content） |
| `token` | 正式回复 token |
| `tool_start` | 工具调用开始 |
| `tool_end` | 工具调用完成 |
| `done` | 本轮对话结束 |

## 完整对话流程

```
User Input
  │
  ├─► [Skills] SkillRetriever.retrieve(query) → 匹配相关技能
  │
  ├─► [Memory] Memory.get_context(query) → 获取历史上下文
  │
  ├─► 构建增强 prompt = system_prompt + 技能提示
  │
  ├─► [LLM/Graph] 执行 LangGraph Agent
  │       ├─► LLM 推理（thinking + content）
  │       ├─► [Tools] 工具调用（如需要）
  │       └─► 循环至最终回复
  │
  ├─► [Memory] 更新记忆（含 reasoning_content）
  │
  ├─► [Skills] 记录技能使用情况
  │
  └─► [Skills] 异步触发反思（fire-and-forget，不阻塞响应）
```

## 反思机制

反思通过 `asyncio.ensure_future()` 异步执行，不阻塞用户响应：
- 每 5 轮对话自动触发
- 新领域（无技能匹配）时触发
- 用户负反馈时立即触发
