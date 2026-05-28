# Alex 未来演进路线图

> **文档定位**：本文档是基于 Alex 当前模块化单体 v2.3 架构（Phase 4 完成）和 `design.md` 中"个人智能工作系统"业务终局，整理出的演进功能点建议。
>
> 每条建议都对照现有代码定位衔接点，避免脱离实现空谈。
>
> **基线版本**：2026-05-28 (Phase 4 完成后)
> **对应业务阶段**：从「终端原生 AI 助手」 → 「可订阅的后台代理」 → 「会成长的个人智能体」 → 「个人智能工作系统」

---

## 当前能力快照

| 能力域 | 现状 | 主要差距 |
|--------|------|---------|
| 工具 | 12+ 内置工具 + MCP Client + 用户插件 + 权限策略 + 审计日志 | 无 embedding 检索工具匹配 |
| 技能 | tag + keyword 检索，LLM 反思/合并稳定 | 无 embedding、无组合技能、无 provenance、无 A/B |
| Cron | APScheduler 异步调度，subscribe 流式推送 | 任务定义不落盘、无变化检测、无重试策略 |
| 可观测性 | TUI bubble + AuditLogger 审计日志 | 无 token/成本统计、无 trace、无 replay |
| 多入口 | 仅 TUI | 无 API、无 headless、无 daemon |
| 安全 | shell deny list + PermissionPolicy + AuditLogger | 无 secret 扫描、明文落盘、shell 无沙箱 |
| 稳定性 | 258/258 回归测试 | `SkillStore`/`SessionRepository` truncate-then-write，无原子写 |

---

## 演进主题总览

```
┌─────────────────────────────────────────────────────────────┐
│                  最终目标：个人智能工作系统                     │
└─────────────────────────────────────────────────────────────┘
                              ▲
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   长期协作能力          后台代理能力           能力进化
   ─ 会话搜索/分支       ─ Cron 持久化          ─ 技能 embedding
   ─ 多模态              ─ 变化检测             ─ 组合技能
                         ─ 事件触发             ─ A/B 影子模式
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                  扩展底盘（横向放大现有能力）
                  ─ MCP Client（一夜放大工具生态）
                  ─ Tool 插件机制
                  ─ FastAPI / Headless / Daemon
                  ─ 可观测性（token/cost/trace/replay）
                              │
                  稳定性底座（先于扩张落实）
                  ─ 原子写 + JSON schema
                  ─ Contract tests
                  ─ Secret 扫描 + 加密
```

---

## 一、工具生态（最能放大「个人工作 OS」感）

**对应业务目标**：从工具集合升级为个人工作操作系统。

### 现状

- 12+ 内置工具：`time` / `web_search` / `web_fetch` / `cron` / `fs_read` / `fs_write` / `edit` / `glob` / `grep` / `git_inspect` / `bash` / `pwsh` + 2 内置（`load_skill` / `cron_history`）
- MCP Client（`tools/mcp_client.py`）自动发现 `~/.alex/mcp.json` 中的 MCP Server
- 用户插件（`tools/plugin_loader.py`）自动扫描 `~/.alex/plugins/*.py`
- 权限策略 + 审计日志（`PermissionPolicy` + `AuditLogger`），副作用工具弹 modal 确认
- LangGraph 默认 `asyncio.gather` 并行调用多个工具
- `ToolExecutionContext` 已是一等运行时上下文（Phase 4 完成）

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 1.1 | ~~MCP Client 支持~~ ✅ | `ToolRegistry` + `mcp_client.py` | 已完成：stdio 协议适配，自动装载 `~/.alex/mcp.json` 中的 MCP Server |
| 1.2 | ~~Tool Plugin Loader~~ ✅ | `plugin_loader.py` 扫描 `~/.alex/plugins/*.py` | 已完成：三种入口约定（`ALEX_TOOLS` / `tools()` / `register(agent)`） |
| 1.3 | ~~本地能力工具~~ ✅ | `fs.py` / `shell.py` / `search.py` / `git.py` | 已完成：fs_read / fs_write / edit / grep / glob / git_inspect / bash / pwsh |
| 1.4 | ~~工具权限/审批~~ ✅ | `PermissionPolicy` + `AuditLogger` + `confirm_screen.py` | 已完成：四级权限 + TUI confirm modal + 审计日志 |
| 1.5 | ~~Tool 调用并行 fan-out~~ ✅ | LangGraph 默认 `asyncio.gather` | 已完成：同一 turn 内多个独立 tool call 自动并行 |

---

## 二、技能系统（差异化核心）

**对应业务目标**：从即时能力升级为经验沉淀。

### 现状

- `SkillRetriever` 用 tag + keyword
- LLM 反思/合并稳定
- `SkillStore` JSON 持久化 + Jinja2 模板

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 2.1 | Embedding 检索升级 | `SkillRetriever` 加 `EmbeddingRetriever` 实现 | pattern + name 入库，余弦相似度 + 现有 tag 加权混合排序（design.md roadmap 已列） |
| 2.2 | 技能链 / 组合技能 | `Skill.composes_of: list[skill_id]` | 高层技能调用多个原子技能，配合 `load_skill` 递归展开 |
| 2.3 | 技能 A/B 与影子模式 | `Skill.status` 增加 `SHADOW` | 新技能先不真注入 prompt，跑 N 次根据反馈再升 ACTIVE，降低反思引入坏技能风险 |
| 2.4 | 技能 provenance | `Skill` 关联 episodes/turn_id | `/skills why <id>` 反查"为什么会有这条技能"，调试关键 |
| 2.5 | 技能导入/导出 + 技能包 | `SkillStore` 增加 import/export | 单机 JSON 先做，再做精选 skill pack（编程/调研/写作），是"跨用户共享"的低成本前置 |
| 2.6 | 反思失败回退 | `SkillStore` 加版本快照 | 反思后置信度持续下降时自动回滚到上一个 version |

---

## 三、Cron / 后台代理

**对应业务目标**：从被动响应升级为主动服务。

### 现状

- APScheduler 异步调度（`CronManager`）
- 任务定义和执行历史均不落盘（design.md 约束第 5 条）
- subscribe=true 时结果以流式对话注入 TUI

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 3.1 | **Cron 任务持久化（重新评估约束）** | `CronManager` + 新增 `~/.alex/cron/jobs.json` | 现状约束写"任务易失"，但用户业务诉求是"持续跟踪"。建议把 *任务定义* 落盘，每次启动重建。约束第 5 条改为「执行历史不落盘到 BaseMessage 流」 |
| 3.2 | 变化检测器 | `CronJob.last_result_hash` | "每小时查一次某网页，有变化才提醒"。和上次 result 一致就静默，省 token + 减少噪音 |
| 3.3 | 事件触发器 | 扩展 `CronManager` 触发器类型 | 除时间触发，加文件系统 watch / webhook，让 cron 真正变成"事件驱动后台代理" |
| 3.4 | 失败重试 + 指数退避 | `CronJob.retry_policy` | 当前失败一次靠下次定时重试，应支持显式重试策略 |
| 3.5 | DAG / 任务依赖 | 进阶项 | A 任务结果作为 B 任务输入，优先级低于持久化 |

---

## 四、可观测性与成本

**对应业务目标**：成长性和长期可用性的前提。

### 现状

- 靠 print 和 TUI bubble 看流
- 无 metrics、无 trace、无审计

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 4.1 | Token / 成本看板 | `LLMGateway` 包 `MeteredLLM` | 记录 prompt_tokens / completion_tokens / cost，写 `~/.alex/metrics/usage.jsonl`。`/usage` 给出按天/会话/技能的统计 |
| 4.2 | OpenTelemetry trace | LangGraph 节点 + tool 调用 | 每个 turn 一条 trace，本地 Jaeger 调试 latency（design.md roadmap 已列） |
| 4.3 | Replay 模式 | `alex --replay <session_id>` | 基于已有 session JSON 对同一输入重跑 LLM 比较输出差异，调优技能/prompt 极其有用 |
| 4.4 | 副作用审计日志 | 副作用工具强制写日志 | 记录 who/when/args/result，加密可选 |

---

## 五、多入口扩展（design.md 已点名"必须支持"）

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 5.1 | FastAPI + SSE | 复用 `AsyncEventBus` | 一个 `WebStreamSubscriber` 把 stream events 转 SSE。design.md roadmap 已列 |
| 5.2 | Headless / Pipe 模式 | `alex --once "问题" --json` | 输出结构化结果，用于 shell 管道、git hook、CI。和 TUI 共用 `Agent` facade |
| 5.3 | VS Code / Cursor 扩展 | stdio 协议（类似 LSP） | IDE 内直接调 Agent |
| 5.4 | 后台 daemon + IPC | `alex serve` | 后台跑 cron 和长连接，多个 TUI/IDE/Web 客户端共享一个 Agent 实例，是"常驻终端"的真正终态 |

---

## 六、对话与会话体验

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 6.1 | 会话搜索 / 标签 | `/search <query>` + SQLite FTS | 跨所有 session 全文检索 |
| 6.2 | 分支与重试 | `SessionRepository.fork(session_id, at_turn)` | 在某条 user message 处 fork 新 session，研究场景的核心需求 |
| 6.3 | 消息编辑 + 重生成 | `ChatHistory` + `Memory` | 现状只能 `/clear`，细粒度"删除最后一轮重答"提升日常体验 |
| 6.4 | 多模态输入 | `display.md` 已列 image/document drag-in | 落实即可 |

---

## 七、安全与隐私（推到生产前必须）

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 7.1 | Secret 扫描 | 写入 skill/session 前过 `detect-secrets` 规则集 | 命中就 redact |
| 7.2 | 本地加密会话 | `~/.alex/sessions/` 用 OS keyring 派生 key 加密 | 避免明文落盘 |
| 7.3 | 沙箱 shell 工具 | `bash` / `pwsh` 默认进 `bubblewrap` / Windows job object | 限制网络和文件系统范围 |

---

## 八、稳定性与基础设施（v2.3 紧接的事）

> 这部分对齐 [refactor-modular-architecture.md](./refactor-modular-architecture.md) 的 Phase 5，建议立即做。

### 演进项

| # | 项目 | 衔接点 | 说明 |
|---|------|--------|------|
| 8.1 | `SkillStore` 原子写 | `SkillStore._save()` | 当前 truncate-then-write，进程崩溃会破坏 skills.json。改 `tempfile + os.replace` |
| 8.2 | `SessionRepository` 原子写 + JSON schema 校验 | `store/session.py` | 同上 |
| 8.3 | Skill JSON 损坏隔离 | `SkillStore._load()` | 当前整文件 `try/except pass`，单条坏数据清空所有技能。改按条加载，坏的进 `skills.corrupt.json` |
| 8.4 | Contract tests | Phase 5 已列入 | `SessionRepository`、`SkillServicePort` |
| 8.5 | CronManager 时区健壮性 | `CronManager._build_cron_trigger` | 当前 `astimezone()` 没显式时区，分布式部署/容器会出诡异行为 |

---

## 优先级矩阵

按 **业务增益 / 实现成本** 综合打分：

### P0（立即做）

> 直接放大现有差异化能力，且改动收敛。

| # | 项目 | 主题 |
|---|------|------|
| 2.1 | 技能 embedding 检索 | 技能 |
| 3.1 | Cron 任务持久化 | Cron |
| 4.1 | Token / 成本看板 | 可观测性 |
| 8.1 | `SkillStore` 原子写 | 稳定性 |
| 8.2 | `SessionRepository` 原子写 | 稳定性 |

### P1（紧接其后）

> 把"长期协作"和"可订阅后台"做实。

| # | 项目 | 主题 |
|---|------|------|
| 3.2 | 变化检测器 | Cron |
| 5.1 | FastAPI + SSE | 多入口 |
| 6.1 | 会话搜索 | 会话 |

### P2（中期）

> 体验和差异化二阶提升。

| # | 项目 | 主题 |
|---|------|------|
| 2.2–2.3 | 技能链 + A/B 影子 | 技能 |
| 4.3 | Replay 模式 | 可观测性 |
| 5.4 | daemon + IPC | 多入口 |
| 6.4 | 多模态输入 | 会话 |
| 2.5 | 技能导入/导出 + 技能包 | 技能 |

### P3（远期）

> 等核心稳定后再扩。

| # | 项目 | 主题 |
|---|------|------|
| - | 跨用户技能市场 | 技能 |
| 5.3 | IDE 插件 | 多入口 |
| - | 声音 I/O | 多入口 |
| - | 多 agent 协作 | 架构 |

---

## 推荐第一步

如果只能先做一件事，**首推 [2.1] 技能 embedding 检索**。理由：

1. `SkillRetriever` 当前用 tag + keyword 匹配，复杂查询容易漏召回
2. embedding 可以语义理解用户意图，提升技能匹配准确率
3. 与现有 `SkillRetriever` 权重接口兼容，可以做混合排序，渐进升级

---

## 与现有架构文档的关系

| 文档 | 关系 |
|------|------|
| [design.md](./design.md) | 业务终局和架构总览，本文档对 §"后续演进方向" 做详细展开 |
| [refactor-modular-architecture.md](./refactor-modular-architecture.md) | 模块化重构蓝图，本文档 §八 与其 Phase 5 保持一致 |
| [skills.md](./skills.md) | 技能系统现状，本文档 §二 在其上扩展 |

---

## 一句话总结

> Phase 4 完成后，Alex 的架构底盘已经稳固。下一阶段的重心要从"内部清理"转向"业务能力扩张"——通过 **MCP / 技能 embedding / Cron 持久化 / 可观测性** 四条主线，把 v2.3 推向「会成长的个人智能体」阶段。
