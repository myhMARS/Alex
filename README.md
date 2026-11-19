# Alex — TypeScript rewrite

Alex 的 **TypeScript 实现**（原 Python 版本已于 2026-11-19 按授权移除）。核心约定：

1. 整体代码使用 TypeScript / Bun；
2. 事件总线（event bus）是模块间唯一通信通道；
3. **TUI 本身也是一个 Module**，订阅 UI 事件、向总线发布用户命令，不经过 WebSocket。

session 使用最新 v3 append-only JSONL 事件日志（不兼容旧 Python/JSON 快照）；skills/cron 仍保持 snake_case 兼容。

## 当前进度

| 层 | 当前实现 | 状态 |
|---|---|---|
| Kernel 消息类型 | `src/kernel/message.ts` | ✅ |
| Event bus | `src/kernel/bus.ts` | ✅ agent scope + serial/waterfall/parallel |
| ModuleHost | `src/kernel/runtime.ts` | ✅（含环检测） |
| 契约 / DTO | `src/kernel/contracts`、`dto` | ✅ 首批 |
| PromptModule | `src/prompts/module.ts` | ✅ 无内置 base；用户 prompt + AGENTS.md + modules 事件组装 |
| StoreModule | `src/modules/store/module.ts` | ✅（v3 JSONL 事件日志） |
| **TuiModule** | `src/modules/tui/module.ts` + `view-state.ts` | ✅ 总线模块 + 视图折叠；渲染器待接入 |
| **AgentModule** | `src/modules/agent/module.ts` | ✅ ReAct 工具循环（LLM → tool_calls → ExecuteTool → tool 消息）+ EchoResponder 离线模式 |
| **Tools / permissions** | `src/tools/` | ✅ registry、权限策略、审批事件、审计、插件加载 + 32 个工具（含 15 个 team 工具） |
| **AgentRegistry** | `src/agents/module.ts` | ✅ spawn/list/dispose/message、team tools |
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

`--demo` 会启动 bus + session/store/tools/agent/TUI，模拟一轮流式对话，打印 TUI 视图折叠结果、工具目录数量，并在临时目录写一次 session（不污染 `~/.alex`）。

## 设计约定

- **类身份分发**：事件/请求用 class 标识，bus 用 `instanceof` 匹配，等价于 Python 的 `isinstance` 订阅。
- **三种语义**：`Event`（广播）、`Command`（单播动作）、`Request<T>`（带返回值）。
- **Agent scope**：消息带 `agentId` / `parentAgentId` / `teamId` / `targetAgentId`；AgentModule 支持 `agentId/parentAgentId/teamId` 并自动注入所有事件；订阅可按 agent/session/team 过滤；定向命令只由目标 agent 处理，串行队列按 agent 而不是仅 session。
- **分发模式**：`publish`（异步串行）、`serial`（顺序执行、首个 bail 值停止）、`waterfall`（中间件 + next，可不调 next 否决）、`parallel`（并发等待）。
- **团队事件契约**：`AgentTeamMessage`、`AgentTeamTaskRequested`、`AgentTeamTaskCompleted`、`AgentTeamMemberJoined/Left`。
- **AgentRegistryModule（`src/agents/module.ts`）**：`SpawnAgent` / `ListAgents` / `GetAgent` / `DisposeAgent` / `SendAgentMessage`；支持 parent/team、AgentSpawned/Disposed、成员加入/离开；AgentModule 停止时自动退订 Bus 监听；已接入 entry，主 Agent 作为 `main` 被 adopt，worker 默认只处理定向命令；注册 `spawn_agent`（支持 `wait:false` 异步 task）/ `wait_agents` / `cancel_agent_task` / `send_agent_message` / `read_agent_mailbox` / `ack_agent_messages` / `mailbox_status` / `replay_agent_mailbox` / `list_dead_letters` / `revive_dead_letters` / `task_status` / `set_team_limits` / `list_agents` / `dispose_agent` / `set_agent_permissions` 工具；per-agent 权限隔离 read/write/shell/network；不同 worker 的 task 并行，同一 worker 串行；任务状态通过 `AgentTaskStarted/Finished` 事件发布，TUI `/agents` 查看并随 task 事件实时刷新；取消 parent task 会向子 Agent 传播，dispose parent 会级联释放子 Agent；task 历史写入 `~/.alex/team/tasks.jsonl`，重启后 running 任务标记为 interrupted/cancelled；工具权限可按 agent 用 `SetAgentPermissions` 覆盖；消息写入 `~/.alex/team/mailbox.jsonl`，ack 写入 `mailbox.jsonl.acks.jsonl`，支持 unread 查询与消费位点（last_read）；Agent 处理完消息后自动 ack，未 ack 消息可 replay，超过尝试次数的进入 dead letter，可 revive 后重新 replay；取消信号通过 `ExecuteTool.signal` 传入 `ToolExecutionContext`，shell 工具中止子进程；fork session 在 header/meta 中记录 `parent_session_id`、`forked_at`、`fork_boundary_seq`；task 记录 `parentSessionId` 关联发起工作的 session；`ListAgents` 可按 teamId 过滤，跨 team 消息直接拒绝；`maxAgentsPerTeam` 限制单 team 成员数，`maxConcurrentTasksPerTeam` 限制单 team 并发 task；`spawn_agent` 支持 `priority`，team 饱和时高优先级 task 先执行；失败 task 记录 `failureReason`（timeout/cancelled/tool_error/agent_error）；`AgentTeamTaskRequested`/`AgentTeamTaskCompleted` 已由 Agent 实际处理；`SetTeamLimits` 可在运行时调整单 team 的成员数与并发上限；agent 消费失败会计入 mailbox attempts，超限自动进入死信；`/team` 过滤同时作用于 sessions 与 lineage；`/task <id>` 联动 task 与 parent session 事件；工具调用即使自身忽略 signal，也会在 abort 时由 ToolsModule 返回取消；TUI 主对话按 `viewAgentId` 只显示当前 Agent，侧边栏 AGENTS 页可切换运行中的 main/worker 视图，切回 main 会重新加载完整主会话；`/sessions` 与恢复列表只显示 main 会话，子 Agent 会话仅在 `/lineage` 诊断可见，不提供 resume；turn 失败与工具 Error 输出会在对话/工具块中直接显示，会话加载失败会弹 toast；`wait_agents` 遵守 `timeout_ms`；请求前用 `ensureToolCallResponses` 修复悬空 tool_calls，避免 provider 400；ExecuteTool 的 bus 超时按工具 `timeoutSeconds + 10s` 设置，不再被默认 30s 提前截断；ChatClient 识别 DeepSeek DSML 工具调用文本并转换为 `tool_calls`，不再把原始标记当正文显示；Agent 执行工具时携带 agentId/parentAgentId/teamId，per-agent 权限策略真正生效；`spawn_agent` 支持 `retries` / `retry_delay_ms` 与跨 team 委派；mailbox 消费失败原因随 attempts 持久化并显示在 dead letter。同一 Agent 同一 session 的 turn 串行排队（不并发/不打断）；`dispose_agent` 并发幂等，字符串 `"false"` 不会误触发 dispose；TUI 支持鼠标选中文本后 `Ctrl+C` 复制（无选区时仍为退出）；TUI `/new` 新建会话（生成新 session 并清空视图，保留旧会话可 resume）；TUI `/mailbox` 查看未读/已读消息；`/lineage` 展示 Agent 树与 Session fork 树，SessionMeta 从 `turn_started` 派生 `agentId`；task 的 started/finished 以 `agent_task_started/finished` 写入 parent session，实现 task↔session 双向引用。
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
| session 文件 | `~/.alex/sessions/<id>.jsonl`，首行 header + 事件行 | 只支持最新 v3；旧格式会明确报错 |

## Parity

最终 parity 矩阵与保留差异见 [PARITY.md](PARITY.md)。`tests/parity.test.ts` 使用冻结的 Python 契约/工具快照（`tests/fixtures/python-contracts.json`）在每次 `bun test` 时校验对齐。

## 迁移阶段

- **P0（已完成）**：kernel、bus、host、核心契约、session/store、TuiModule + 测试。
- **P1（已完成）**：tools registry + 权限/审批/审计 + 内置工具（time/read/write/edit/glob/grep/git_inspect/bash/pwsh/web_fetch/web_search）+ 插件加载器。
- **P2（已完成）**：MCP client（stdio / streamable-http，含 SSE 响应解析、工具目录合并、`InvokeProviderTool` 路由）；周期健康检查待补。
- **P3（已完成）**：LLM ChatClient、Agent ReAct 循环、Skill 检索/加载/反思提炼。
- **P4（进行中）**：Cron / durable jobs 已完成；prompts 模板与 config 完整化待补。
- **P5（已完成）**：进程内 OpenTUI 渲染器、CLI（`--tui/--demo/--help/--version/--session/--no-mcp`）与 `bun link` 安装；单文件二进制的 OpenTUI 原生依赖限制已确认（需随包 node_modules）。
- **P6（已完成）**：parity 矩阵与自动化对齐测试、TUI 命令页/diff 预览/MCP 健康检查；2026-11-19 已按授权移除 Python legacy 并将 TS 提升到仓库根目录。

## Demo

```bash
bun run src/entry.ts --demo
# modules started: settings → prompt → session → store → tools → agents → mcp → cron → skill → agent → tui
# agent mode: echo (no ALEX_API_KEY)
# view: done "Echo: hello from the bus" (user)
# history messages: 2
# sessions persisted: 1
# tools in catalog: 17
# mcp: No MCP servers configured
```

说明：TUI 模块通过 `submitUserTurn()` 发命令 → AgentModule 订阅并流式发布事件 → SessionModule 追加事件 → Store 落 JSONL → TUI 视图折叠。整条链路没有任何 WebSocket。

## 测试

当前 285 个测试（41 个文件），覆盖：

- bus：启动前缓冲、子类事件分发、handler 隔离、会话/Agent 串行、agent-scoped 订阅、定向事件、serial/waterfall/parallel、request/reply、超时/无 handler/异常包装、shutdown。
- host：依赖顺序、TUI 最后启动、启动失败回滚、循环依赖检测、可选依赖告警、逆序停止。
- token-meter：DSH 风格字符/块/role 启发式估算，供压缩与上下文预算使用。
- store：事件即时追加 JSONL、会话列表、bundle 加载、`created_at` 保留、v3 格式校验。
- TUI module：流式 token/thinking/skill/tool 折叠、turn 完成/失败、反思与 cron 事件、命令发布（`UserTurnRequested` / `ToolApprovalResolved`）。
- AgentModule：echo turn、token 流、失败 → `TurnFailed`、后台任务不阻塞 dispatch loop、`whenIdle()` 语义。
- Tools：registry、插件加载（`pluginDirs` → `register(registry)`）、权限策略（allow/deny/ask/remember）、catalog、read（offset/limit/max_bytes/encoding/越根拒绝）、write 审批通过/拒绝/超时 + 审计、edit（file_path/encoding）、glob、grep（glob/context/count/ignore_case/type/head_limit）、shell（cwd/timeout_seconds）安全策略、pwsh 执行、web_fetch HTML 转文本、DDG 结果解析、git_inspect。
- TUI module：流式折叠之外新增审批队列（`pendingApprovals`）。
- LLM：配置/env 覆盖、`.env` 解析、SSE 分片解析（content / thinking / tool-call 增量 / usage）、HTTP 错误。
- Agent model loop：流式 token、工具调用执行与结果回填、session projection 消息序列（user → assistant(tool_calls) → tool → assistant）、TUI 视图折叠、失败 → `TurnFailed`、达到 maxIterations 后强制收尾。
- Skill 合并：LLM consolidation（merged_groups / deprecate_ids 容错解析、keeper 更新与重复删除、无模型时确定性 fallback）。
- Prompts：`PromptModule` 汇总用户 prompt（无内置 base）/ AGENTS.md / modules sections；Tools、MCP、Skill 通过 `RegisterPromptSection` / `UnregisterPromptSection` 事件组装与卸载；`/prompt` 查看，`/reload-prompts` 重载；用户可编辑 `~/.alex/system-prompt.md`（或 `ALEX_SYSTEM_PROMPT_PATH`）。
- Skill：Python 格式 snake_case 兼容、检索打分（tag/name/pattern + CANDIDATE 惩罚）、retrieve/load/list/usage/delete/deprecate、`load_skill`/`list_skills` 工具注册与执行、LLM 反思（新建/更新/废弃 + SkillsReflected）、无模型时 SkillReflectError、负反馈触发反思、JSON 容错解析。
- Agent + Skill：SkillModule 在用户 turn 按检索结果注册临时的 skills prompt section，turn 结束后卸载；`load_skill` → SkillLoaded + tool 消息；有技能时不自动反思、无技能时自动反思并落库。
- Parity：冻结快照中 41 个 kernel 契约与 15 个内建工具名全部对齐；session 仅支持最新 v3 JSONL。
- CLI：auto/tui/demo 模式、`--session` 两种写法、互斥参数与未知参数校验、help/version。
- TUI 展开/Toast：Ctrl+T/K/O 切换 thinking/skills/工具输出、toast 自动过期、侧栏快捷键提示。
- TUI 代码高亮：TS/Python/JSON/Bash 关键字/字符串/注释/数字着色；Ctrl+T/K/O、气泡样式、`:q` 全角冒号兼容。
- 上下文窗口页：`/context`（或 `/ctx`）直接使用 provider 上报的 token 用量：上下文占用（最近 prompt / max_context）、本次会话累计输入/输出、缓存命中/未命中与命中率、最近请求明细及最近 8 条消息摘要；不再显示估算拆分。
- 记忆窗口：按估算 token 预算裁剪；估算参考 DSH token-meter 启发式（4 字符/token + 每块 4 + 每条消息 4），CJK 因 4 字符/token 会低估而按 1.5 字符/token 校正；消息条数上限仅作安全兜底。
- Provider 用量：Agent 在每次模型请求结束后发布 `TokenUsageReported`，`/context` 显示真实 prompt/completion/total tokens（无上报时提示）。
- 图片/文件消息：可直接复制文件路径粘贴到输入框（自动进入待发送附件栏，Enter 发送）；截图后按 F6 可直接读取剪贴板图片并附加；也支持 `/image <path>`、`/file <path>` 或正文内联 `@<path>`；图片经 Bun.Image 校验、像素预算缩放（2048×2048）、WebP 质量阶梯（85/75/60，目标 4MB）、按 sha256 去重存储到 `~/.alex/attachments/images`；请求时按 `image_max`（默认 2048）生成并缓存路由变体；历史只保存 `alex-attachment:<hash>` 引用，发送前校验摘要并投影为 data URL；普通文件复制到 `~/.alex/attachments` 并按 DSH 风格只把可读路径交给模型（模型用 read 工具按需读取，不内联内容）；对话区显示附件标签。
- 模型强度：`/effort nothinking|low|high|max` 即时切换并持久化；状态栏显示当前模型、effort 与 provider 上下文占用百分比；`/settings` 可开关 thinking。
- 上下文压缩：达到 `compact_threshold`（默认 0.8）时自动把较早历史摘要为一条消息并保留近期尾部；`/compact` 手动触发；对话区显示压缩提示、条数与释放 token 数。
- 设置页：`/settings` 查看 provider/model/API Key（脱敏）/base_url/max_context/max_output/compact_threshold/thinking/effort/max_iterations；`/set max_context 1M`、`/set max_output 64K`、`/set compact_threshold 0.8`、`/set max_iterations 20` 修改，支持 K/M 单位（K=1024、M=1,048,576，也可输入精确值）并持久化到 `~/.alex/settings.json`（0600）；max_iterations 由 `ALEX_MAX_ITERATIONS` 提供默认值，修改后即时同步到 main/worker agent（其余设置重启后生效）。
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
- 会话投影：`SessionModule` 是唯一 seq 来源，维护带 `seq` 的 append-only JSONL 事件日志；`projectSessionSurface` 投影 messages/rawMessages/turns/request header/headerSeq/customEvents；Agent 生产路径只读投影、不再双写 Memory；resume 恢复 usage/缓存/压缩卡片、surface turn 文本/附件，并把上次 system prompt/tools/thinking/effort 精确回填下一次请求；每条 assistant/tool 消息通过 `headerSeq` 关联 request header；插件/自定义事件保留在 `customEvents`；写入后自动 fsync；`/fork` 支持按事件 seq 前缀复制。
- session 损坏：JSONL 头部/事件损坏会报错并 Toast；`/repair` 从首个坏事件截断并原子重写；`/checkpoint` 刷盘；`/log` 查看含压缩前原始事件的完整日志；只支持 v3，旧版本明确拒绝。
- 反馈：Ctrl+G / Ctrl+B → `FeedbackSubmitted`（带最后一轮 turnId）。
- 审批预览：`write`/`edit` 生成 unified diff（新增/修改/上下文/截断）。
- TUI 渲染：OpenTUI headless 渲染（header/turns/工具/反思状态）、输入提交 → `UserTurnRequested`、审批弹窗 + y/n 快捷键 → `ToolApprovalResolved`、Ctrl+C 退出。
- Cron：表达式解析/非法校验、next-run 计算（步长/日期/星期）、snake_case durable 文件、one-shot 清理、RUNNING 重置、定时触发与重排、取消、重启恢复、失败标记、`CronTurnRequested` → Agent cron turn → `TurnCompleted` 关联、cron 工具端到端。
- MCP：配置解析（stdio / http / disabled）、SSE 响应解析、stdio 端到端（连接 → tools/list → tools/call）、不可达服务器降级、streamable-http 握手 + session id 复用、`ToolsProvided` → 工具目录合并 → `ExecuteTool` 路由。
- 集成测试：`TuiModule` 命令 → `AgentModule` → session/store → TUI 视图 + session 落盘全链路；TUI 审批队列 → `ToolApprovalResolved` → write 工具执行。