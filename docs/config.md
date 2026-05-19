# 配置管理 (`alex/config.py`)

## 业务逻辑

1. 通过 `python-dotenv` 从 `.env` 文件加载环境变量
2. 返回 `LLMConfig` 对象供 `LLMFactory.create()` 使用
3. `.env` 文件不提交到版本控制，`.env_example` 作为模板

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ALEX_PROVIDER` | LLM 提供商 | `deepseek` |
| `ALEX_API_KEY` | API 密钥 | *(必填)* |
| `ALEX_BASE_URL` | API 基础地址 | `https://api.deepseek.com` |
| `ALEX_MODEL` | 模型名称 | `deepseek-chat` |
| `ALEX_MAX_TOKENS` | 最大 token 数 | `4096` |
| `ALEX_TEMPERATURE` | 温度参数 | `0.0` |

## 优先级

```
环境变量 > .env 文件 > 默认值
```

（已注入的系统环境变量会覆盖 `.env` 文件中的值）

## 与现有代码的关系

- 返回 `LLMConfig` 对象，增加 provider 字段
- 统一使用环境变量方式管理配置，不再依赖 `.apikey` 文件