# 配置管理 (`alex/config.py`)

## 业务逻辑

1. 从 `.apikey` 文件读取配置（provider、baseurl、apikey、models）
2. 环境变量优先级高于文件（`ALEX_PROVIDER`、`ALEX_API_KEY`、`ALEX_BASE_URL`）
3. 返回 `LLMConfig` 对象供 `LLMFactory.create()` 使用

## 配置文件格式

```
provider:deepseek
baseurl:https://api.deepseek.com
apikey:sk-xxx
models:deepseek-chat,deepseek-reasoner
```

## 优先级

```
环境变量 > .apikey 文件 > 默认值
```

## 与现有代码的关系

- 返回 `LLMConfig` 对象，增加 provider 字段
- 保持向后兼容，原有配置文件格式继续支持
