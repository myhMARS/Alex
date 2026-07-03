# 工具生态 (`alex/tools/` + `alex/mcp/`)

## 设计目标

让 Alex 在终端里"动手"做事，同时保持安全可控：
- 工具按需扩展（内置 + 用户插件 + MCP）
- 副作用工具默认不放开（权限策略 + 配置驱动）
- `ToolsModule` 作为统一网关，通过 bus 暴露工具目录和执行能力
- MCP 作为独立模块（`alex/mcp/`），后台连接并广播工具到网关

---

## 架构分层

```
                    Agent / AgentModule
                          │
            bus.request(ExecuteTool)
            bus.request(GetToolCatalog)
                          │
                          ▼
              ┌─────────────────────┐
              │    ToolsModule      │  ← 统一网关
              │  (provides handlers) │
              └──────────┬──────────┘
                         │
            ┌────────────┼────────────┬───────────────┐
            ▼            ▼            ▼               ▼
       内置工具       用户插件      MCPModule        cron 工具
       (builtin)     (~/.alex/    (alex/mcp/)      (通过 bus
                      plugins/)                    request)
```

`ToolsModule` 启动时：
1. 注册 5 个 request handler：`GetToolCatalog`, `ExecuteTool`, `InvokeProviderTool`, `RegisterTool`, `UnregisterTool`
2. 订阅 `ToolsProvided`（收编 MCP/plugin 工具）和 `ToolApprovalResolved`（权限确认回执）
3. 注册所有内建工具（time, web, fs, shell, cron 等）
4. 广播 `ToolsProvided` 事件（内建工具目录）

### MCP 模块独立化

`alex/mcp/` 是独立的 MCP 模块（`MCPModule`），不再属于 `alex/tools/`。启动时在**定时任务**中连接 MCP servers，连接成功后通过 `ToolsProvided` 事件广播工具 spec，`ToolsModule` 收编到统一目录。

这样设计的好处：
- MCP 连接不影响启动速度（慢的 MCP server 不会阻塞 TUI）
- 工具执行统一走 `ToolsModule`，MCP 模块只负责连接管理和实际调用

---

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `ToolsModule` | `tools/module.py` | bus 网关：目录管理、执行路由、权限确认 |
| `ToolRegistry` | `tools/registry.py` | 按名称管理工具 |
| `ToolExecutor` | `tools/executor.py` | 执行工具 + 权限检查 + 错误归一化 |
| `AlexTool` | `tools/models.py` | 自定义工具类（替代 LangChain StructuredTool） |
| `PermissionPolicy` | `tools/permissions.py` | 权限策略（环境驱动，通过 bus 事件做 confirm） |
| `plugin_loader` | `tools/plugin_loader.py` | 用户插件发现与装载 |
| `MCPModule` | `mcp/module.py` | MCP 模块入口：后台连接 + ToolsProvided 广播 |
| `mcp_client` | `mcp/mcp_client.py` | MCP 多 transport 客户端 |

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

### 权限确认流程（完全通过 bus 事件）

```
ToolsModule._handle_execute()
  ├─ 检查权限 → 已在 allowed 集合 → 直接执行
  ├─ 已在 denied 集合 → 返回 blocked
  └─ 需要确认 →
      ├─ bus.publish(ToolApprovalRequested(req_id, tool_name, preview))
      ├─ TUI 订阅 → 展示确认弹窗
      ├─ 用户选择 → TUI 发布 bus.publish(ToolApprovalResolved(req_id, granted, remember))
      └─ ToolsModule 收到回执 → 继续执行或拒绝
```

### 配置策略

| 来源 | 优先级 | 说明 |
|------|--------|------|
| `ALEX_TOOL_PERMISSIONS` 环境变量 | 高 | 逗号分隔，如 `read,write,shell` |
| `ALEX_TOOL_DENY` 环境变量 | 最高 | 逗号分隔，最后应用 |
| 默认 `DEFAULT_ALLOWED` | 低 | `{read, network}` |

---

## 内置工具一览

| 工具 | 权限 | 主要约束 |
|------|------|---------|
| `time` | — | 时区别名 + ISO 8601 |
| `web_search` | `network` | DDG，最多 15 条结果 |
| `web_fetch` | `network` | 长度上限，跳过 script/style |
| `cron` | — | 通过 bus.request(ScheduleCron) 调度，5 字段 cron；`prompt` 必须只含真实任务内容 |
| `load_skill` | — | SkillModule 注册：按名加载技能详情 |
| `list_skills` | — | SkillModule 注册：浏览可用技能 |
| `cron_jobs` | — | 通过 bus.request(ListCronJobs) 查询 cron 任务列表 |
| `read` | `read` | 限定 allowed roots，二进制拒绝，自动截断 |
| `write` | `write` | tempfile + os.replace 原子写，限大小，限 root |
| `edit` | `write` | 精确字符串替换，必须先 `read`/`write` 过；外部修改检测 |
| `glob` | `read` | 按文件名 glob 找文件，按 mtime 倒序 |
| `grep` | `read` | 正则内容搜索，优先 `rg`，否则纯 Python fallback |
| `git_inspect` | `read` | 仅 status/diff/log 三个只读 action |
| `bash` | `shell` | 命令字符串走 `bash -lc`，硬性 deny list |
| `pwsh` | `shell` | 命令字符串走 `pwsh -NoProfile -NonInteractive -Command` |

---

## 用户插件机制

### 发现规则

- 默认目录：`~/.alex/plugins/`
- 扫描 `*.py`，跳过 `_*.py`
- 每个文件作为独立模块加载，失败隔离

### 入口约定

按优先级匹配：

1. `ALEX_TOOLS = [tool, tool, ...]`（模块级常量）
2. `def tools() -> list[BaseTool]`（工厂函数）
3. `def register(agent) -> None`（自由注册）

---

## MCP 客户端 (`alex/mcp/`)

### 设计

MCP 模块独立于 tools，同时支持两类接入方式：

- `command` 型：通过 stdio 启动本地 MCP server 子进程
- `url` 型：通过 HTTP 连接远端 MCP server，默认使用 `streamable-http`
- 显式兼容旧式 `transport: "sse"` 配置

### 连接生命周期

- `MCPModule.start()` → 创建后台 `asyncio.Task` 连接所有 MCP servers
- `MCPClientPool.connect_all()` 通过 `AsyncExitStack` 持有所有 transport 上下文
- 每个 server 独立连接，失败不影响其他 server
- 连接成功后通过 `bus.publish(ToolsProvided(provider="mcp", specs=...))` 广播
- `ToolsModule._on_tools_provided()` 收编到统一目录
- `MCPModule.stop()` → 取消连接任务，关闭 pool

### 工具命名

每个 MCP 工具被适配为：

- 名称：`mcp__<server>__<tool>`（小写）
- 权限：`network`
- metadata：`mcp_server` / `mcp_tool` 字段供调试

### 配置

`~/.alex/mcp.json`（配置解析在 `alex/config.py`，不依赖 MCP SDK）：

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
        "Authorization": "Bearer token-value"
      },
      "timeout": 15
    }
  }
}
```

---

## 完整调用链

```
LangGraph / ChatAppService 决定调用工具
  │
  ▼
TurnProcessor → bus.request(ExecuteTool(name, args, ctx))
  │
  ▼
ToolsModule._handle_execute()
  ├─ 查找工具（registry 或 provider_specs）
  ├─ 权限检查（_check_permission_via_bus）
  │   ├─ 已允许 → 执行
  │   ├─ 已拒绝 → 返回 blocked
  │   └─ 需确认 → bus.publish(ToolApprovalRequested)
  │       → 等待 bus 回执 → 继续/拒绝
  ├─ bus.publish(ToolStarted)
  ├─ tool.invoke(args)
  ├─ bus.publish(ToolFinished)
  └─ return ToolResult
```

---

## 目录结构

```
alex/tools/
├── __init__.py            # 公共导出
├── module.py              # ToolsModule — bus 网关
├── registry.py            # ToolRegistry
├── executor.py            # ToolExecutor + 权限检查
├── models.py              # AlexTool — 自定义工具类
├── permissions.py         # PermissionPolicy + AuditLogger
├── plugin_loader.py       # 用户插件装载
├── _path.py               # resolve_path_in_allowed_roots
├── _binary.py             # looks_like_binary
├── time.py
├── web_search.py
├── web_fetch.py
├── cron.py
├── fs.py                  # read / write / edit / FileReadTracker
├── shell.py               # bash / pwsh
├── search.py              # grep / glob
└── git.py                 # git_inspect

alex/mcp/
├── __init__.py
├── module.py              # MCPModule — 后台连接 + ToolsProvided 广播
└── mcp_client.py          # MCP 多 transport 客户端 + tool 适配
```

---

## 测试覆盖

| 文件 | 覆盖范围 |
|------|---------|
| `tests/test_tools.py` | web_search / web_fetch 元数据 |
| `tests/test_tools_registry.py` | Registry / Executor 基础 |
| `tests/test_permissions.py` | Policy 默认值、env 覆盖、confirm hook、tool gating |
| `tests/test_audit_log.py` | AuditLogger 追加、写失败容错、决策记录 |
| `tests/test_approval_summariser.py` | summariser 附加 / 失败降级 / write diff / bash + pwsh 摘要 |
| `tests/test_fs_tools.py` | read / write 边界、原子性、二进制拒绝 |
| `tests/test_edit_tool.py` | read-before-edit、外部修改检测、唯一性、replace_all |
| `tests/test_search_tools.py` | grep / glob 各种 output_mode / 过滤 |
| `tests/test_shell_tool.py` | bash + pwsh 元数据、deny list、解释器检测 |
| `tests/test_git_tool.py` | status/diff/log + 越界拒绝 |
| `tests/test_plugin_loader.py` | 三种入口、坏插件隔离 |
| `tests/test_mcp_client.py` | 配置解析、stdio/HTTP transport、schema 转换 |
| `tests/test_confirm_screen.py` | 权限确认 modal 渲染 |
| `tests/test_markdown_rendering.py` | render_response 行为、finalize 切 Markdown |
| `tests/test_time_tool.py` | time 工具时区别名 + ISO 8601 |
| `tests/test_cron.py` | cron prompt 调度 / durable 恢复 / 执行历史 |
| `tests/test_crontab.py` | 5 字段 crontab 表达式解析 |
| `tests/test_tui.py` | TUI 渲染 / session 生命周期 / 工具气泡顺序 |

总计：325 测试。
