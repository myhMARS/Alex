# 工具生态 (`alex/tools/`)

## 设计目标

让 Alex 在终端里"动手"做事，同时保持安全可控：
- 工具按需扩展（内置 + 用户插件 + MCP）
- 副作用工具默认不放开（权限策略 + 配置驱动）
- 任何一种来源的工具都接入同一个 `ToolRegistry`，对 Agent 透明

---

## 架构分层

```
┌──────────────────────────────────────────────────────────┐
│                      Agent / ChatAppService              │
└──────────────────────────────────────────────────────────┘
                          │
                          ▼
                  ┌──────────────┐         ┌─────────────────┐
                  │ ToolRegistry │ ◄──────►│  ToolExecutor   │
                  └──────────────┘         │ + Permissions   │
                          ▲                 └─────────────────┘
                          │
            ┌─────────────┼─────────────┬───────────────┐
            ▼             ▼             ▼               ▼
       内置工具        本地能力        用户插件         MCP 客户端
       (web/time/    (fs/shell/    (~/.alex/       (~/.alex/
        cron)         git)          plugins)        mcp.json)
```

所有工具最终都是 LangChain `StructuredTool` 实例，通过 `metadata` 字段声明所需权限。

---

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `ToolRegistry` | `tools/registry.py` | 按名称管理工具 |
| `ToolExecutor` | `tools/executor.py` | 执行工具 + 权限检查 + 错误归一化 |
| `ToolExecutionContext` | `tools/ports.py` | 一等运行时上下文（session_id / source / metadata） |
| `PermissionPolicy` | `tools/permissions.py` | 权限策略（环境驱动，可注入 confirm hook） |
| `plugin_loader` | `tools/plugin_loader.py` | 用户插件发现与装载 |
| `mcp_client` | `tools/mcp_client.py` | MCP 服务器适配（stdio + HTTP） |

---

## 权限模型

### 权限等级

| 标识符 | 语义 | 默认放开 |
|--------|------|---------|
| `read` | 纯信息读取（read / git_inspect / grep / glob） | ✅ |
| `network` | 出网（web_search / web_fetch / mcp） | ✅ |
| `write` | 改用户磁盘状态（write / edit） | ❌ |
| `shell` | 调用外部进程（bash / pwsh） | ❌ |
| `danger` | 显式高风险操作 | ❌ |

### 工具声明权限

通过 `StructuredTool.metadata`：

```python
StructuredTool.from_function(
    coroutine=_write,
    name="write",
    args_schema=FsWriteInput,
    metadata={"required_permission": PERMISSION_WRITE},
)
```

`ToolExecutor.execute()` 会在调用 `tool.ainvoke()` 之前查询 `PermissionPolicy.check()`。被拒绝的调用统一返回 `Error: tool '<name>' blocked: <reason>`，模型可见、可重试。

### 配置策略

| 来源 | 优先级 | 说明 |
|------|--------|------|
| `ALEX_TOOL_PERMISSIONS` 环境变量 | 高 | 逗号分隔，如 `read,write,shell` |
| `ALEX_TOOL_DENY` 环境变量 | 最高 | 逗号分隔，最后应用 |
| `PermissionPolicy.confirm_hook` | 中 | 异步回调，让 TUI 弹确认框 |
| 默认 `DEFAULT_ALLOWED` | 低 | `{read, network}` |

`PermissionPolicy.from_env()` 在 agent 启动时调用一次，结果传给 `ChatAppService`。`Agent.set_permissions(policy)` 可在运行时替换策略（用于 TUI 接入 confirm 提示）。

`remember_grants=True`（默认）会缓存 confirm hook 的成功授权直到 agent 关闭，避免重复打扰。

### 审批请求 (`ToolApprovalRequest`)

每次 gated 调用都会构造一个 `ToolApprovalRequest`：

| 字段 | 用途 |
|------|------|
| `tool_name` / `permission` | 标识被请求的工具和权限 |
| `args` | 原始参数（深拷贝） |
| `summary` | 单行人话摘要（"Edit /path/x (+3 / -1, 102 bytes total)"） |
| `preview` | `PreviewBlock` 列表，每块带 `title` / `body` / `kind`（`text` / `code` / `diff`） |

工具可调用 `attach_approval_summariser(tool, summariser)` 注册一个 async summariser：

```python
async def _summarise(args: dict) -> tuple[str, list[PreviewBlock]]:
    return "Edit foo.py", [PreviewBlock(title="diff", body=..., kind="diff")]
```

返回值支持 `str` / `(summary, preview)` / 完整 `ToolApprovalRequest`。Summariser 抛错时降级为提示文本，不阻塞用户决策。

### TUI 确认对话框

`PermissionConfirmScreen`（modal）渲染 `ToolApprovalRequest`：

| 按键 | 结果 |
|------|------|
| `Y` / `A` | Allow once — 仅本次允许 |
| `S` | Allow always — 本会话内不再询问（写入 `policy.allowed`） |
| `N` / `Esc` | Deny — 工具立即返回 `Error: ... blocked` |

`write` / `edit` 摘要会自动 read 旧文件 → `difflib.unified_diff` → 在 modal 里高亮显示，CRLF/LF 差异自动归一化避免噪音。
`bash` / `pwsh` 摘要展示完整命令字符串、cwd、超时秒数。

### 审计日志 (`AuditLogger`)

每次决策都会写一条到 `~/.alex/audit/permissions.jsonl`：

```json
{
  "ts": 1706512345.123,
  "iso": "2024-01-29T05:52:25.123+00:00",
  "tool": "write",
  "permission": "write",
  "decision": "allow_once",
  "args_digest": "Edit /path/x (+3 / -1, 102 bytes total)",
  "reason": ""
}
```

`decision` 取值：`auto_allow` / `auto_deny` / `allow_once` / `allow_always` / `deny`。

写入异步走 `asyncio.to_thread`，失败被吞掉——审计日志故障**永不**阻塞工具执行。`AuditLogger.read_all()` 提供一个同步读接口便于事后排查。

---

## 内置工具一览

| 工具 | 权限 | 主要约束 |
|------|------|---------|
| `time` | — | 时区别名 + ISO 8601 |
| `web_search` | `network` | DDG，最多 15 条结果 |
| `web_fetch` | `network` | 长度上限，跳过 script/style |
| `cron` | — | 用 5 字段 cron 调度 prompt 驱动的后台任务，可选 durable 持久化；`prompt` 必须只包含真实任务内容，不能带提醒包装、到时措辞、状态说明或装饰性文本；持久化只负责重启恢复，恢复后绑定当前会话，不会在 Alex 关闭后后台执行 |
| `load_skill` | — | 内置：按名加载技能详情 |
| `cron_jobs` | — | 内置：查询当前 cron 任务列表，包含 durable 任务 |
| `read` | `read` | 限定 allowed roots，二进制拒绝，自动截断；同时刷新 `FileReadTracker` |
| `write` | `write` | tempfile + os.replace 原子写，限大小，限 root；写完同步 tracker |
| `edit` | `write` | 精确字符串替换，必须先 `read`/`write` 过；外部修改会被检测并拒绝；唯一性检查（除非 `replace_all`） |
| `glob` | `read` | 按文件名 glob 找文件，按 mtime 倒序返回，最多 200 条 |
| `grep` | `read` | 正则内容搜索，优先调用系统 `rg`，否则纯 Python fallback；支持 `content`/`files_with_matches`/`count` 三种 output_mode |
| `git_inspect` | `read` | 仅 status/diff/log 三个只读 action |
| `bash` | `shell` | 命令字符串走 `bash -lc`，支持管道 / 重定向 / `&&`；启动前对解析后的 token 做硬性 deny list（rm/dd/sudo/...） |
| `pwsh` | `shell` | 命令字符串走 `pwsh -NoProfile -NonInteractive -Command`，缺失时回退 `powershell.exe`；deny list 含 Remove-Item / Format-Volume / Stop-Computer / iex 等

### `read` / `write` / `edit` 协作

三者共享同一个 `FileReadTracker`，记录每个文件的 `mtime_ns + size + sha256` 指纹：

- `read` 成功 → 写入指纹
- `write` 成功 → 写入指纹（agent 知道写完后的状态）
- `edit` 调用前查指纹：未读过 → 拒绝；指纹与磁盘当前状态不符 → 拒绝（要求重新读）

这避免两类典型错误：
1. 模型在没看过文件时凭空 edit
2. 文件被外部进程或同会话其他工具改过，模型仍按旧内容做替换

`edit` 的语义和 Claude 风格一致：`old_string` 必须**在文件中精确匹配且唯一**，否则拒绝；想替换多处需显式 `replace_all=true`，且 `old_string == new_string` 视为 no-op 拒绝。

### `grep`

优先使用 `rg`：

- 自动尊重 `.gitignore`、`.ignore`
- 支持 `-t TYPE`（py/ts/md…）、`-i`、`-A/-B/-C`、`--multiline`
- LLM 通过 `pattern` / `output_mode` / `glob` / `type` / `head_limit` 等字段调用

未安装 ripgrep 时退回纯 Python：

- `os.walk` 跳过 `.git`/`node_modules`/`__pycache__`/`.venv` 等常见噪音
- 跳过 >2 MiB 的单文件，跳过二进制
- `output_mode='content'` 时支持前/后置 context，复用滑动窗去重相邻 match
- 截断时尾部加 `... (results truncated; raise head_limit to see more)`

### `glob`

- 委托 `pathlib.Path.glob`
- 仅返回 `is_file()` 的路径
- 按 `mtime` 倒序，默认最多 200 条
- `path` 缺省 → 第一个 allowed root

### `bash` / `pwsh`

替代了早期单一的 `shell_run` 工具——argv-only 调用方式无法表达管道、重定向、`cd && ...`、PowerShell 的 cmdlet 语法。现在按解释器拆成两个独立工具：

| 维度 | `bash` | `pwsh` |
|------|--------|--------|
| 入参 | `command: str`（命令字符串）+ `cwd` + `timeout_seconds` | 同左 |
| 解释器 | `bash -lc <cmd>`，需 PATH 中有 `bash`（Linux/macOS/WSL/Git Bash） | `pwsh -NoProfile -NonInteractive -Command <cmd>`，缺失则回退 `powershell.exe` |
| 解析 | `shlex.split(posix=True)` 后逐 token 校验 | 正则 `[A-Za-z][\w-]*` 抓 token，case-insensitive 比对 |
| 硬性 deny list | rm/rmdir/dd/mkfs/format/sudo/su/shutdown/reboot/chmod/chown/halt/poweroff | Remove-Item/ri/rd/Format-Volume/Stop-Computer/Restart-Computer/Set-Acl/Invoke-Expression/iex/rm/dd/sudo/... |
| 输出 | stdout + stderr 各自截断到 32 KiB |

启动器 `create_available_shell_tools()` 自动检测并仅注册当前主机存在的解释器，所以 Linux-only / Windows-only 部署都不需要写平台分支。

modal 摘要展示完整命令、cwd、超时秒数；deny list 在 spawn 之前就拦截，永远不会真的把破坏性命令交给解释器。

#### 使用建议

- 跨平台脚本：模型自行判断主机能力，优先调 `pwsh`（Windows）或 `bash`（其余），需要时显式指定
- Git 操作：`git status` / `git log` 用 `git_inspect`；`commit` / `push` 等仍走 `bash` 或 `pwsh`

### `git_inspect`

- 仅 `status` / `diff` / `log` 三个只读动作
- `commit` / `push` / `reset` 等需要走 `bash` 或 `pwsh`（要求 `shell` 权限）

---

## 用户插件机制

### 发现规则

- 默认目录：`~/.alex/plugins/`
- 扫描 `*.py`，跳过 `_*.py`
- 每个文件作为独立模块加载（`importlib.util.spec_from_file_location`），失败隔离

### 入口约定

按优先级匹配：

1. `ALEX_TOOLS = [tool, tool, ...]`（模块级常量）
2. `def tools() -> list[BaseTool]`（工厂函数）
3. `def register(agent) -> None`（自由注册）

```python
# ~/.alex/plugins/my_tool.py
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class _Input(BaseModel):
    name: str = Field(description="who to greet")


async def _hello(name: str) -> str:
    return f"Hello, {name}!"


ALEX_TOOLS = [
    StructuredTool.from_function(
        coroutine=_hello,
        name="hello",
        description="Say hello",
        args_schema=_Input,
    ),
]
```

### 加载时机

`create_agent()` 默认在装配时调用 `install_plugins(agent)`，每个插件返回一个 `PluginLoadResult`，包含 `path / tools / registered_via / error`。错误不阻塞其他插件，主入口可自行决定如何上报。

---

## MCP 客户端

### 设计

Alex 的 MCP client 同时支持两类接入方式：

- `command` 型：通过 stdio 启动本地 MCP server 子进程
- `url` 型：通过 HTTP 连接远端 MCP server，默认使用 `streamable-http`
- 显式兼容旧式 `transport: "sse"` 配置

连接成功后统一调用 `list_tools()` 拿到 schema，封装为 `StructuredTool`，登记到 `ToolRegistry`。对 Agent 来说，stdio / HTTP 只是 transport 差异，工具注册与调用路径保持一致。

`mcp` SDK 现在属于项目主依赖，正常安装 Alex 时会一并安装。

如果运行环境缺少 `mcp` 包，`load_mcp_tools_from_config()` 仍会抛 `MCPUnavailableError`，启动器把错误转成 toast 提示，避免 MCP 故障阻塞 TUI 启动。

### 配置

`~/.alex/mcp.json`：

```json
{
  "mcpServers": {
    "local-server": {
      "command": "your-mcp-command",
      "args": ["--your-arg", "value"],
      "env": {}
    },
    "http-server": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer token-value",
        "X-Custom-Header": "custom-value"
      },
      "timeout": 15
    },
    "sse-server": {
      "transport": "sse",
      "url": "http://localhost:8123/sse",
      "headers": {"Authorization": "Bearer token-value"},
      "timeout": 15,
      "sse_read_timeout": 60,
      "disabled": false
    }
  }
}
```

字段约定：

| 字段 | 适用 transport | 说明 |
|------|----------------|------|
| `command` | `stdio` | 本地可执行程序 |
| `args` | `stdio` | 子进程参数列表 |
| `env` | `stdio` | 传给子进程的环境变量 |
| `url` | `streamable-http` / `sse` | 远端 MCP server 地址 |
| `headers` | `streamable-http` / `sse` | 注入到 HTTP 请求的头 |
| `transport` | 全部 | 可选；支持 `stdio`、`streamable-http`、`sse` |
| `timeout` | `streamable-http` / `sse` | HTTP 客户端总超时 |
| `sse_read_timeout` | `streamable-http` / `sse` | 流式读取超时，主要用于 SSE/长连接 |
| `disabled` | 全部 | `true` 时跳过连接 |

推断规则：

- 只给 `command`，默认按 `stdio` 处理
- 只给 `url`，默认按 `streamable-http` 处理
- `transport: "http"` 会被归一化成 `streamable-http`
- `transport: "http-sse"` 会被归一化成 `sse`

### 工具命名

每个 MCP 工具被适配为：

- 名称：`mcp__<server>__<tool>`（小写）
- 描述：`[MCP:<server>] <原描述>`
- 权限：`network`
- metadata：`mcp_server` / `mcp_tool` 字段供调试

### 生命周期

- `MCPClientPool.connect_all()` 通过 `AsyncExitStack` 持有所有 transport 上下文和 `ClientSession`
- stdio server 持有 `stdio_client(...)` 子进程上下文
- HTTP server 持有 `streamable_http_client(...)` 或 `sse_client(...)` 连接上下文
- HTTP 模式下会为 SDK 注入 `httpx.AsyncClient` 工厂，用于合并 `headers`、`timeout`、`sse_read_timeout`
- TUI 在 `_do_shutdown()` 调用 `pool.aclose()` 统一回收子进程
- 单个 server 连接失败被记录到 `MCPConnection.error`，其他 server 不受影响

---

## 完整调用链

```
LangGraph 决定调用工具
  ↓
ToolExecutor.execute(ctx, name, args)
  ├─ registry.get(name)        → 查找工具
  ├─ required_permission(tool) → 读 metadata
  ├─ permissions.check(...)    → 同步/异步授权
  │     └─ (可选) confirm_hook → TUI 弹确认
  └─ tool.ainvoke(args)        → 实际执行
                ↓
            字符串结果 / Error: ...
                ↓
            回到 LangGraph，模型继续推理
```

---

## 并行 fan-out

LangGraph 的 `create_agent` 在同一 turn 内对多个工具调用已经默认走 `asyncio.gather` 并行，无需额外改造。延迟敏感场景（多次 `web_search` + `web_fetch`）会自然受益。

---

## 目录结构

```
alex/tools/
├── __init__.py            # 公共导出
├── ports.py               # ToolExecutionContext / Protocol
├── registry.py            # ToolRegistry
├── executor.py            # ToolExecutor + 权限检查
├── permissions.py         # PermissionPolicy + 内置常量
├── plugin_loader.py       # 用户插件装载
├── mcp_client.py          # MCP 多 transport 客户端 + tool 适配
├── time.py
├── web_search.py
├── web_fetch.py
├── cron.py
├── fs.py                  # read / write / edit / FileReadTracker
├── shell.py               # bash / pwsh
├── search.py              # grep / glob
└── git.py                 # git_inspect
```

---

## 测试覆盖

| 文件 | 覆盖范围 |
|------|---------|
| `tests/test_tools.py` | web_search / web_fetch 元数据 |
| `tests/test_tools_registry.py` | Registry / Executor 基础 |
| `tests/test_permissions.py` | Policy 默认值、env 覆盖、confirm hook（once/always）、tool gating、executor 不双重 prompt |
| `tests/test_audit_log.py` | AuditLogger 追加、写失败容错、auto_allow/auto_deny/allow_once/allow_always 决策记录、digest |
| `tests/test_approval_summariser.py` | summariser 附加 / 失败降级 / write diff（创建 / 编辑 / no-op / CRLF / 二进制 / 越界） / bash + pwsh 摘要 / 端到端 gate→hook→write |
| `tests/test_fs_tools.py` | read / write 边界、原子性、二进制拒绝 |
| `tests/test_edit_tool.py` | read-before-edit、外部修改检测、唯一性、replace_all、空字符串拒绝、端到端确认/拒绝 |
| `tests/test_search_tools.py` | grep（files/content/count/context/type/glob 过滤/越界/无效正则/head_limit）、glob（mtime 倒序/越界/默认 path/空 pattern） |
| `tests/test_shell_tool.py` | bash + pwsh 元数据、deny list（鉴别 token / 别名 / 大小写）、cwd 越界、超时、空命令、host 检测；解释器缺失时整组测试自动 skip |
| `tests/test_git_tool.py` | status/diff/log + 越界拒绝（依赖本机 git，缺失则 skip） |
| `tests/test_plugin_loader.py` | 三种入口、坏插件隔离、空入口报错 |
| `tests/test_mcp_client.py` | 配置解析、stdio/HTTP transport 连接、schema 转换、SDK 缺失、disabled、tool 调用 |
| `tests/test_confirm_screen.py` | 权限确认 modal 底部按键提示渲染（防止 Rich markup 吞 `[Y]` `[N]` 等字符） |
| `tests/test_markdown_rendering.py` | `render_response` 行为、`bubble.finalize` 切到 Markdown、流式期间保持纯文本、`insert_tool` 提交 prefix 也走 Markdown |
| `tests/test_time_tool.py` | time 工具时区别名 + ISO 8601 输出 |
| `tests/test_cron.py` | cron prompt 调度 / durable 恢复 / 执行历史 |
| `tests/test_crontab.py` | 5 字段 crontab 表达式解析与校验 |
| `tests/test_tui.py` | TUI 渲染 / session 生命周期 / 工具气泡顺序 / 消息序列保真 |

总计：258 / 258 通过。
