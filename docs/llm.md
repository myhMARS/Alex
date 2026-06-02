# LLM 工厂层 (`alex/llm/`)

## 设计思路

工厂模式 + 装饰器注册。每个 provider 是一个独立文件，通过 `@LLMFactory.register("provider_name")` 自动注册。所有适配器继承 LangChain 的 `BaseChatModel`，与 LangGraph 无缝集成。

## 核心组件

| 组件 | 职责 |
|------|------|
| `LLMConfig` | 统一配置数据类（provider、api_key、base_url、model、temperature 等） |
| `LLMFactory` | 工厂类，维护 provider → adapter 的注册表，`create(config)` 生产实例 |
| 各 Adapter | 继承对应 LangChain ChatModel，实现 `from_config(cls, config)` 类方法 |

## 业务逻辑

1. `LLMFactory.register(name)` — 装饰器，将 adapter 类注册到工厂
2. `LLMFactory.create(config)` — 根据 `config.provider` 查找注册表，实例化对应 adapter
3. 各 adapter 负责将通用 `LLMConfig` 映射为各平台特有参数
4. DeepSeek adapter 额外处理 `reasoning_content` 回传（thinking mode 支持）

## 扩展方式

新增 provider = 新增一个文件 + 一个装饰器，零侵入。

## client — 统一 LLM 客户端

`client.py` 提供两种调用方式：

- **`ChatClient`** — 实例化客户端，支持 `stream_chat()`（流式对话）和 `json_completion()`（JSON 模式补全）
- **`create_json_completion()`** — 模块级便捷函数，内部创建 `ChatClient`，用于反射和技能合并等无需长期持有 client 的场景

两者共享相同的 provider 处理逻辑：
- 使用 OpenAI SDK 的 `response_format: {"type": "json_object"}` 确保 JSON 输出
- DeepSeek provider 自动通过 `extra_body` 禁用 thinking mode（避免混入 reasoning_content）
- 接受可选 `config: LLMConfig` 参数，`None` 时回退到 `get_llm_config()`
- 返回纯 JSON 字符串，由调用方配合 `json_repair` 解析

## 目录结构

```
alex/llm/
├── __init__.py       # ChatClient / create_json_completion 导出
├── factory.py        # LLMFactory — ChatClient 构造
├── base.py           # LLMConfig 数据类
└── client.py         # ChatClient（streaming + JSON-mode）
```
