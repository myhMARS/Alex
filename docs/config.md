# 配置管理 (`alex/config.py`)

## 业务逻辑

1. 通过 `python-dotenv` 从 `.env` 文件加载环境变量（搜索顺序：`CWD/.env` → `~/.alex/.env` → 默认 dotenv 搜索）
2. 返回 `LLMConfig` 对象供 `LLMFactory.create()` 使用
3. 提供工具权限、Cron 调试、日志等配置读取函数
4. `.env` 文件不提交到版本控制，`.env_example` 作为模板

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ALEX_PROVIDER` | LLM 提供商 | `deepseek` |
| `ALEX_API_KEY` | API 密钥 | *(必填)* |
| `ALEX_BASE_URL` | API 基础地址 | `https://api.deepseek.com` |
| `ALEX_MODEL` | 模型名称 | `deepseek-chat` |
| `ALEX_MAX_TOKENS` | 最大 token 数 | `8192` |
| `ALEX_TEMPERATURE` | 温度参数 | `0.0` |
| `ALEX_CRON_DEBUG` | Cron 调试日志开关（`1`/`true` 开启） | *(关闭)* |
| `ALEX_TUI_MARKDOWN` | TUI Markdown 渲染开关（`0` 关闭） | *(开启)* |
| `ALEX_TOOL_PERMISSIONS` | 允许的工具权限（逗号分隔，如 `read,write,shell`） | `read,network` |
| `ALEX_TOOL_DENY` | 显式拒绝的工具权限（逗号分隔） | *(空)* |
| `ALEX_MCP_DEBUG` | MCP 调试日志开关 | *(关闭)* |
| `ALEX_LOG_MAX_BYTES` | 日志文件最大字节数 | `5242880` (5 MiB) |
| `ALEX_LOG_BACKUP_COUNT` | 日志文件备份数量 | `5` |

## LLMConfig 数据类

`LLMConfig` 是通用的 LLM 配置数据类，定义在 `alex/llm/base.py`：

| 字段 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `provider` | `str` | provider 标识符 | `"deepseek"` |
| `api_key` | `str` | API 密钥 | `""` |
| `base_url` | `str` | API 基础地址 | `"https://api.deepseek.com"` |
| `model` | `str` | 模型名称 | `"deepseek-chat"` |
| `max_tokens` | `int` | 最大 token 数 | `8192` |
| `temperature` | `float` | 采样温度 | `0.0` |
| `extra` | `dict` | 额外的 provider 特定参数 | `{}` |

`extra` 字段允许传递 provider 特有的配置参数。

## MCP 配置

`config.py` 提供纯配置解析（不依赖 MCP SDK），`MCPServerConfig` 是纯 dataclass：

```python
@dataclass
class MCPServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    sse_read_timeout: float | None = None
    enabled: bool = True
```

`load_mcp_config(path)` 解析 `~/.alex/mcp.json`，支持 transport 别名归一化（`http` → `streamable-http`，`http-sse` → `sse`）。

## 工具函数一览

| 函数 | 用途 |
|------|------|
| `get_llm_config()` | 构建 LLMConfig |
| `get_env_str(name, default)` | 读取字符串环境变量 |
| `get_env_bool(name, default)` | 读取布尔环境变量 |
| `get_env_int(name, default)` | 读取整数环境变量 |
| `get_env_float(name, default)` | 读取浮点环境变量 |
| `get_env_csv_set(name)` | 读取逗号分隔值为 set |
| `is_tui_markdown_enabled_by_default()` | Markdown 开关 |
| `is_cron_debug_enabled()` | Cron 调试开关 |
| `is_mcp_debug_enabled()` | MCP 调试开关 |
| `get_allowed_permissions(defaults)` | 允许的权限集合 |
| `get_denied_permissions()` | 拒绝的权限集合 |
| `get_log_max_bytes(default)` | 日志最大字节数 |
| `get_log_backup_count(default)` | 日志备份数 |
| `load_mcp_config(path)` | MCP 配置解析 |

## 优先级

```
环境变量 > .env 文件 > 默认值
```

（已注入的系统环境变量会覆盖 `.env` 文件中的值）
