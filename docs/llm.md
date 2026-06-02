# LLM 客户端 (`alex/llm/`)

## 设计思路

基于 **OpenAI Python SDK** 的统一 `ChatClient`，取代早期的 LangChain adapter 模式。单个 `ChatClient` 类通过 `LLMConfig` 配置支持 DeepSeek、OpenAI、Anthropic 等多 provider —— 不再需要每个 provider 写一个 adapter 类。

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `LLMConfig` | `base.py` | 统一配置数据类（provider、api_key、base_url、model、temperature 等） |
| `LLMFactory` | `factory.py` | 工厂类，`create(config)` 生产 `ChatClient` 实例 |
| `ChatClient` | `client.py` | 统一客户端：`stream_chat()` 流式对话 + `json_completion()` JSON 模式补全 |

## ChatClient

`client.py` 提供两种调用方式：

- **`stream_chat(messages, tools)`** — 流式对话，返回 async generator，逐 token yield `(delta_content, reasoning_content)` 元组
- **`json_completion(prompt, system_prompt)`** — JSON 模式补全，用于技能反思、合并等非对话场景
- **`create_json_completion(prompt, ...)`** — 模块级便捷函数，内部创建 ChatClient

### 流式对话

```python
client = ChatClient(config)
async for delta, reasoning in client.stream_chat(messages, tools=tool_schemas):
    if reasoning:
        # DeepSeek thinking mode → bus.publish(ThinkingUpdated)
    if delta:
        # 正常 token → bus.publish(TokenEmitted)
```

### JSON 模式

- 使用 OpenAI SDK 的 `response_format: {"type": "json_object"}` 确保 JSON 输出
- DeepSeek provider 自动通过 `extra_body` 禁用 thinking mode（避免混入 reasoning_content）
- 接受可选 `config: LLMConfig` 参数，`None` 时回退到 `get_llm_config()`
- 返回纯 JSON 字符串，由调用方配合 `json_repair` 解析

### Provider 处理

与早期 LangChain 适配器模式不同，ChatClient 是 **单一类**，通过 `LLMConfig` 区分 provider：

- `provider="deepseek"` → `base_url` 默认 `https://api.deepseek.com`，启用 `extra_body` 控制 reasoning
- `provider="openai"` → `base_url` 默认 `https://api.openai.com/v1`
- `provider="anthropic"` → 通过 Anthropic 兼容端点接入
- 自定义 provider → 通过 `base_url` + `api_key` 任意配置

## LLMConfig

```python
@dataclass
class LLMConfig:
    provider: str = "deepseek"           # provider 标识符
    api_key: str = ""                     # API 密钥
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    max_tokens: int = 8192
    temperature: float = 0.0
    extra: dict = field(default_factory=dict)  # provider 特有参数
```

## 使用示例

```python
from alex.config import get_llm_config
from alex.llm.client import ChatClient

config = get_llm_config()  # 从环境变量读取
client = ChatClient(config)

# 流式对话
async for delta, reasoning in client.stream_chat(messages, tools=[...]):
    ...

# JSON 补全
from alex.llm import create_json_completion
result = await create_json_completion("分析以下对话...", system_prompt="你是技能分析师")
```

## 目录结构

```
alex/llm/
├── __init__.py       # ChatClient / create_json_completion 导出
├── factory.py        # LLMFactory — ChatClient 构造
├── base.py           # LLMConfig 数据类
└── client.py         # ChatClient (OpenAI SDK, streaming + JSON-mode)
```
