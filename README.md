# Alex — TypeScript rewrite

Alex 的 **TypeScript 实现**（原 Python 版本已于 2026-11-19 按授权移除）。核心约定：

1. 整体代码使用 TypeScript / Bun；
2. 事件总线（event bus）是模块间唯一通信通道；
3. **TUI 本身也是一个 Module**，订阅 UI 事件、向总线发布用户命令，不经过 WebSocket。

落盘格式（session/skills/cron）仍保持 snake_case，兼容旧的 `~/.alex` 数据。

## 当前进度

| 层 | 当前实现 | 状态 |
|---|---|---|
| Kernel 消息类型 | `src/kernel/message.ts` | ✅ |
| Event bus | `src/kernel/bus.ts` | ✅ |
| ModuleHost | `src/kernel/runtime.ts` | ✅（含环检测） |
| 契约 / DTO | `src/kernel/contracts`、`dto` | ✅ 首批 |
| MemoryModule | `src/modules/memory/module.ts` | ✅ |
| StoreModule | `src/modules/store/module.ts` | ✅（文件格式兼容） |
| **TuiModule** | `src/modules/tui/module.ts` + `view-state.ts` | ✅ 总线模块 + 视图折叠；渲染器待接入 |
| **AgentModule** | `src/modules/agent/module.ts` | ✅ ReAct 工具循环（LLM → tool_calls → ExecuteTool → tool 消息）+ EchoResponder 离线模式 |
| **Tools / permissions** | `src/tools/` | ✅ registry、权限策略、审批事件、审计、插件加载 + 17 个工具（参数已对齐 Python） |
| **MCP** | `src/mcp/` | ✅ stdio + streamable-http/SSE 响应、工具合并与调用、状态查询 |
| **Skill / reflection** | `src/skill/` | ✅ JSON 存储、关键词检索、`load_skill`/`list_skills` 工具、LLM 反思提炼、负反馈反思 |
| **Cron** | `src/cron/` | ✅ 5 字段 cron 解析、定时触发、durable 持久化/恢复、`cron`/`cron_cancel`/`cron_jobs` 工具 |
| **LLM client** | `src/llm/` | ✅ OpenAI 兼容流式（content/thinking/tool-call 增量）、.env 加载 |
| **OpenTUI 渲染器** | `src/tui/` | ✅ 进程内直连 TuiModule（无 WS 桥），富 Markdown、代码高亮、气泡、侧栏、鼠标滚轮 |

## 运行

```bash
bun install
bun run typecheck
bun test

# 直接运行
bun run src/entry.ts --tui      # interactive OpenTUI (in-process bus)
bun run src/entry.ts --demo     # headless scripted demo
bun run src/entry.ts --help     # CLI help / --version / --session / --no-mcp

# 安装为 CLI（bun 全局 bin）
bun link
alex-ts --version               # ~/.bun/bin 需在 PATH 中
alex-ts --tui

# 生成可分发 JS bundle（OpenTUI 保持 external，运行时需 node_modules）
bun run build                   # -> dist/alex-ts.js
```

`--demo` 会启动 bus + memory/store/tools/agent/TUI 五个模块，模拟一轮流式对话，打印 TUI 视图折叠结果、工具目录数量，并在临时目录写一次 session（不污染 `~/.alex`）。

## 设计约定

- **类身份分发**：事件/请求用 class 标识，bus 用 `instanceof` 匹配，等价于 Python 的 `isinstance` 订阅。
- **三种语义**：`Event`（广播）、`Command`（单播动作）、`Request<T>`（带返回值）。
- **会话串行**：同一个 `sessionId` 的事件按发布顺序串行处理；不同会话之间由单一 dispatch loop 保持全局顺序。
- **错误隔离**：事件 handler 抛错不影响其他 handler；Request handler 异常包成 `HandlerError`，超时抛 `CapabilityTimeout`。
- **TUI = Module**：`TuiModule` 只依赖 bus；`view-state.ts` 是纯函数式视图折叠，渲染器（OpenTUI）仅消费快照并调用 `submitUserTurn` / `resolveApproval` 等意图方法。

### Python → TS 字段映射

| Python | TS | 说明 |
|---|---|---|
| `session_id` / `turn_id` | `sessionId` / `turnId` | 总线消息字段 |
| `skill_name` / `skill_pattern` | `skillName` / `skillPattern` | |
| `json_schema` | `jsonSchema` | `ToolSpec` |
| `SkillsReflected.new` | `SkillsReflected.created` | `new` 是关键字 |
| `run_id` / `req_id` / `job_id` | `runId` / `reqId` / `jobId` | |
| 落盘 JSON（session 文件） | 保持 `snake_case` | 与现有 `~/.alex/sessions/*.json` 兼容 |

## Parity

最终 parity 矩阵与保留差异见 [PARITY.md](PARITY.md)。`tests/parity.test.ts` 使用冻结的 Python 契约/工具快照（`tests/fixtures/python-contracts.json`）在每次 `bun test` 时校验对齐。

## 迁移阶段

- **P0（已完成）**：kernel、bus、host、核心契约、memory/store、TuiModule + 测试。
- **P1（已完成）**：tools registry + 权限/审批/审计 + 内置工具（time/read/write/edit/glob/grep/git_inspect/bash/pwsh/web_fetch/web_search）+ 插件加载器。
- **P2（已完成）**：MCP client（stdio / streamable-http，含 SSE 响应解析、工具目录合并、`InvokeProviderTool` 路由）；周期健康检查待补。
- **P3（已完成）**：LLM ChatClient、Agent ReAct 循环、Skill 检索/加载/反思提炼。
- **P4（进行中）**：Cron / durable jobs 已完成；prompts 模板与 config 完整化待补。
- **P5（已完成）**：进程内 OpenTUI 渲染器、CLI（`--tui/--demo/--help/--version/--session/--no-mcp`）与 `bun link` 安装；单文件二进制的 OpenTUI 原生依赖限制已确认（需随包 node_modules）。
- **P6（已完成）**：parity 矩阵与自动化对齐测试、TUI 命令页/diff 预览/MCP 健康检查；2026-11-19 已按授权移除 Python legacy 并将 TS 提升到仓库根目录。

## Demo

```bash
bun run src/entry.ts --demo
# modules started: memory → store → tools → mcp → cron → skill → agent → tui
# agent mode: echo (no ALEX_API_KEY)
# view: done "Echo: hello from the bus" (user)
# history messages: 2
# sessions persisted: 1
# tools in catalog: 17
# mcp: No MCP servers configured
```

说明：TUI 模块通过 `submitUserTurn()` 发命令 → AgentModule 订阅并流式发布事件 → Memory/Store 落库 → TUI 视图折叠。整条链路没有任何 WebSocket。

## 测试

当前 197 个测试（30 个文件），覆盖：

- bus：启动前缓冲、子类事件分发、handler 隔离、会话串行、request/reply、超时/无 handler/异常包装、shutdown。
- host：依赖顺序、TUI 最后启动、启动失败回滚、循环依赖检测、可选依赖告警、逆序停止。
- memory：滑动窗口、replace/clear、会话隔离、经 bus 的完整 CRUD。
- store：`TurnCompleted` 持久化、会话列表、bundle 加载、`created_at` 保留、文件格式兼容。
- TUI module：流式 token/thinking/skill/tool 折叠、turn 完成/失败、反思与 cron 事件、命令发布（`UserTurnRequested` / `ToolApprovalResolved`）。
- AgentModule：echo turn、token 流、失败 → `TurnFailed`、后台任务不阻塞 dispatch loop、`whenIdle()` 语义。
- Tools：registry、插件加载（`pluginDirs` → `register(registry)`）、权限策略（allow/deny/ask/remember）、catalog、read（offset/limit/max_bytes/encoding/越根拒绝）、write 审批通过/拒绝/超时 + 审计、edit（file_path/encoding）、glob、grep（glob/context/count/ignore_case/type/head_limit）、shell（cwd/timeout_seconds）安全策略、pwsh 执行、web_fetch HTML 转文本、DDG 结果解析、git_inspect。
- TUI module：流式折叠之外新增审批队列（`pendingApprovals`）。
- LLM：配置/env 覆盖、`.env` 解析、SSE 分片解析（content / thinking / tool-call 增量 / usage）、HTTP 错误。
- Agent model loop：流式 token、工具调用执行与结果回填、memory 消息序列（user → assistant(tool_calls) → tool → assistant）、TUI 视图折叠、失败 → `TurnFailed`、达到 maxIterations 后强制收尾。
- Skill 合并：LLM consolidation（merged_groups / deprecate_ids 容错解析、keeper 更新与重复删除、无模型时确定性 fallback）。
- Prompts：system / reflection / skills-section / merge / skill-card 构建器及测试。
- Skill：Python 格式 snake_case 兼容、检索打分（tag/name/pattern + CANDIDATE 惩罚）、retrieve/load/list/usage/delete/deprecate、`load_skill`/`list_skills` 工具注册与执行、LLM 反思（新建/更新/废弃 + SkillsReflected）、无模型时 SkillReflectError、负反馈触发反思、JSON 容错解析。
- Agent + Skill：检索技能注入 system prompt、`load_skill` → SkillLoaded + tool 消息、有技能时不自动反思、无技能时自动反思并落库。
- Parity：冻结快照中 41 个 kernel 契约与 15 个内建工具名全部对齐、Python 格式 session/skills/cron JSON 可加载。
- CLI：auto/tui/demo 模式、`--session` 两种写法、互斥参数与未知参数校验、help/version。
- TUI 展开/Toast：Ctrl+T/K/O 切换 thinking/skills/工具输出、toast 自动过期、侧栏快捷键提示。
- TUI 代码高亮：TS/Python/JSON/Bash 关键字/字符串/注释/数字着色；Ctrl+T/K/O、气泡样式、`:q` 全角冒号兼容。
- 上下文窗口页：`/context`（或 `/ctx`）直接使用 provider 上报的 token 用量：上下文占用（最近 prompt / max_context）、本次会话累计输入/输出、缓存命中/未命中与命中率、最近请求明细及最近 8 条消息摘要；不再显示估算拆分。
- 记忆窗口：按估算 token 预算裁剪；估算参考 DSH token-meter 启发式（4 字符/token + 每块 4 + 每条消息 4），CJK 因 4 字符/token 会低估而按 1.5 字符/token 校正；消息条数上限仅作安全兜底。
- Provider 用量：Agent 在每次模型请求结束后发布 `TokenUsageReported`，`/context` 显示真实 prompt/completion/total tokens（无上报时提示）。
- 图片/文件消息：可直接复制文件路径粘贴到输入框（自动进入待发送附件栏，Enter 发送）；截图后按 F6 可直接读取剪贴板图片并附加；也支持 `/image <path>`、`/file <path>` 或正文内联 `@<path>`；图片经 Bun.Image 校验、像素预算缩放（2048×2048）、WebP 质量阶梯（85/75/60，目标 4MB）、按 sha256 去重存储到 `~/.alex/attachments/images`，再以 data URL 发送；普通文件复制到 `~/.alex/attachments` 并按 DSH 风格只把可读路径交给模型（模型用 read 工具按需读取，不内联内容）；对话区显示附件标签。
- 模型强度：`/effort nothinking|low|high|max` 即时切换并持久化；状态栏显示当前模型、effort 与 provider 上下文占用百分比；`/settings` 可开关 thinking。
- 上下文压缩：达到 `compact_threshold`（默认 0.8）时自动把较早历史摘要为一条消息并保留近期尾部；`/compact` 手动触发；对话区显示压缩提示、条数与释放 token 数。
- 设置页：`/settings` 查看 provider/model/API Key（脱敏）/base_url/max_context/max_output/compact_threshold/thinking/effort；`/set max_context 1M`、`/set max_output 64K`、`/set compact_threshold 0.8` 修改，支持 K/M 单位（K=1024、M=1,048,576，也可输入精确值）并持久化到 `~/.alex/settings.json`（0600），重启后生效。
- 反思记录页：`/reflections`（或命令面板）列出最近 20 次反思的时间、+新建/~更新/-废弃数量及技能名。
- 侧栏默认收起：启动时只显示对话区，按 Ctrl+S / F1 / F2 或点击展开；
- 侧栏 Tab 支持鼠标点击切换；状态通知改为右下角悬浮圆角气泡（透明背景、自动消失，不挤占布局）。
- STATUS 分区：SESSION / RUNTIME（tools/skills/cron/mcp/reflect 对齐数值）/ KEYS 双列快捷键，弱化分隔线；内容超高时可滚动。
- 侧栏 Tab：STATUS / FILES 两页，F1/F2 或 Ctrl+1/2 切换；FILES 为**懒加载**文件树（展开目录时按需读取，单目录最多 1000 项，不受全局截断影响），↑↓ 选择、→/← 展开收起、Enter 切换，忽略 node_modules/.git 等。
- 文件树刷新：5 秒定时只刷新 STATUS 统计；文件树在切换 FILES / 按 F5 时刷新，避免滚动卡顿；展开状态与选中位置保留。
- 文件树滚动条：仅保留垂直滚动条（深色轨道），隐藏底部横向滚动条。
- Git 状态：仓库根目录自动检测 `.git`，文件树逐文件显示 M/A/D/R/? 状态，目录聚合子项状态并在侧栏显示变更数量。
- Slash 命令面板：输入 `/` 在输入框上方弹出命令列表，↑↓ 选择、窗口滚动、Enter 执行、Esc 关闭，输入后续字符自动过滤。
- 顶部栏 / 空态：单行品牌顶栏（`◆ ALEX · session · tools · ready`）+ 居中 ASCII Logo 欢迎屏；Ctrl+S 收起/展开侧栏。
- 页面滚动：`/sessions` `/skills` 等页面 ScrollBox 绑定终端高度（`flexBasis:0`），支持鼠标滚轮并显示滚动条。
- `/sessions` 可读性：按最近活动时间倒序，每条会话 3 行（ID / 时间+消息数 / 首条消息），页面逐行截断避免横向溢出。
- 审批预览换行：超长单行命令/路径按面板宽度自动换行（CJK 按双宽计算），不再在右侧截断。
- 审批预览分页：预览按面板宽度截断、按终端高度分页，支持 ↑↓ / PgUp/PgDn / Home/End 滚动，避免长 diff 溢出。
- 工具调用可读性：单行参数摘要（`command=… cwd=…`）、JSON 自动缩进、ANSI 清理、超长行截断、折叠显示行数、展开仅前 14 行并提示剩余行数。
- TUI 时间线：助手文本 / thinking / 技能 / 工具调用按事件发生顺序交错渲染（先说→调用→再说）。
- TUI 富 Markdown：heading/list/code/quote/inline 的 span 渲染 + 解析器测试。
- Skill 模板：`prompts/<id>.j2` 写入/更新/删除、与 Python 静态模板内容一致、自定义 promptsDir。
- Skill evolution：CANDIDATE→ACTIVE、失败淘汰、max-active 上限。
- TUI 鼠标滚轮：`createCliRenderer({ useMouse: true })` → ScrollBox 滚轮/点击事件。
- TUI 状态侧栏：tools/skills/cron/mcp/reflection 计数、快捷键提示；宽终端显示、窄终端隐藏。
- TUI 页面：`/help` `/skills` `/cron` `/mcp` `/sessions` 经总线取数、`:q` 关闭；渲染测试覆盖页面打开/关闭。
- MCP legacy SSE 传输：GET 事件流 + endpoint 发现 + POST 请求 / SSE 响应关联。
- TUI markdown：标题/列表/引用/代码块/行内格式的终端化渲染。
- MCP 健康检查：30s 周期 `tools/list` 刷新、工具增删同步、掉线标 ERROR；`/mcp` 页面数字展开/收起 server 工具。
- 会话恢复：`/sessions` 列表 → 数字选择 → `LoadSession` + `ReplaceMemory` + 视图重建；渲染测试验证历史与 header 会话切换。
- 反馈：Ctrl+G / Ctrl+B → `FeedbackSubmitted`（带最后一轮 turnId）。
- 审批预览：`write`/`edit` 生成 unified diff（新增/修改/上下文/截断）。
- TUI 渲染：OpenTUI headless 渲染（header/turns/工具/反思状态）、输入提交 → `UserTurnRequested`、审批弹窗 + y/n 快捷键 → `ToolApprovalResolved`、Ctrl+C 退出。
- Cron：表达式解析/非法校验、next-run 计算（步长/日期/星期）、snake_case durable 文件、one-shot 清理、RUNNING 重置、定时触发与重排、取消、重启恢复、失败标记、`CronTurnRequested` → Agent cron turn → `TurnCompleted` 关联、cron 工具端到端。
- MCP：配置解析（stdio / http / disabled）、SSE 响应解析、stdio 端到端（连接 → tools/list → tools/call）、不可达服务器降级、streamable-http 握手 + session id 复用、`ToolsProvided` → 工具目录合并 → `ExecuteTool` 路由。
- 集成测试：`TuiModule` 命令 → `AgentModule` → memory/store → TUI 视图 + session 落盘全链路；TUI 审批队列 → `ToolApprovalResolved` → write 工具执行。