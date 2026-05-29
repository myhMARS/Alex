# TUI 交互界面 (`alex/tui/`)

## 设计思路

基于 **Textual** 框架构建终端 TUI 应用，运行在 alternate screen buffer 中。采用组件化架构，所有对话内容作为 widget 挂载到可滚动容器中，通过 CSS class 切换实现折叠/展开，避免 DOM 重建导致的页面跳动。

用户 turn 通过 `Agent.chat_stream()` 的 async generator 获取流式事件，cron turn 通过 `AsyncEventBus` 订阅获取。两者的渲染逻辑由共享的 `StreamRenderer` 统一管理。

## 架构布局

```
┌────────────────────────────────────────────────────────────┐
│  Header (标题 + 时钟)                                       │
├───────────────────────────────────────┬────────────────────┤
│  VerticalScroll #chat-view            │ VerticalScroll     │
│  ┌────────────────────────────────┐   │ #status-bar        │
│  │ UserBubble (cyan border)       │   │ ┌──────────────┐   │
│  ├────────────────────────────────┤   │ │ 后台任务       │   │
│  │ AlexBubble (green border)      │   │ │ ⟳ job1...    │   │
│  │  ├─ skills-collapsed/expanded  │   │ │ ⏱ job2...    │   │
│  │  ├─ ToolBubble ×N              │   │ └──────────────┘   │
│  │  ├─ thinking-collapsed/expanded│   │                    │
│  │  └─ response-text              │   │                    │
│  ├────────────────────────────────┤   │                    │
│  │ feedback-prompt (条件显示)      │   │                    │
│  └────────────────────────────────┘   │                    │
├───────────────────────────────────────┴────────────────────┤
│  Input #input-box                                           │
└────────────────────────────────────────────────────────────┘
```

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `AlexApp` | `app.py` | Textual App 主类，wiring center — 装配 projector/notifications/view_state |
| `ChatControllerMixin` | `controller.py` | 命令分发、page 管理、session 生命周期、toggles（343 行）；通过 `_ControllerHost` Protocol 约束对宿主 App 的依赖 |
| `ChatProjector` | `chat_projector.py` | bus→widget 事件投影、cron renderer 管理、status bar、cron history；通过 `_ProjectorHost` Protocol 约束对宿主 App 的依赖 |
| `_ControllerHost` | `ports.py` | TUI structural subtyping Protocol，约束 `ChatControllerMixin` duck typing |
| `NotificationController` | `notification_controller.py` | toast 通知、feedback prompt、rating 提交 |
| `SessionViewState` | `view_state.py` | UI 可变状态 dataclass，`reset()` 统一入口 |
| `StreamRenderer` | `stream_renderer.py` | 共享流式渲染状态管理（用户/cron turn 共用） |
| `UserBubble` | `presenter.py` | 用户消息气泡（cyan 圆角边框） |
| `AlexBubble` | `presenter.py` | AI 回复容器（green 圆角边框），内含 skills/tools/thinking/response |
| `ToolBubble` | `presenter.py` | 单个工具调用展示（实线边框，含参数和结果） |
| `SystemBubble` | `presenter.py` | 系统通知消息（反思结果等） |
| `ChatHistory` | `view_models.py` | 会话视图模型，维护 ChatTurn 列表 + BaseMessage 序列 |
| `ChatTurn` | `view_models.py` | 单轮对话数据模型（含 skills 字段和 kind） |

## 折叠/展开机制

采用 **CSS `display: none` 切换**，不销毁/重建 DOM：

- `AlexBubble` 在 `compose()` / `finalize()` 时同时生成 expanded 和 collapsed 两个版本
- 通过 `.hidden` CSS class（`display: none`）控制哪个可见
- `set_thinking_expanded()` / `set_skills_expanded()` 只切换 class，不触发布局重建

## 快捷键

| 快捷键 | 作用 |
|--------|------|
| `Ctrl+T` | 切换所有 thinking 展开/收起 |
| `Ctrl+K` | 切换所有 skills 展开/收起 |
| `Ctrl+G` | 给最后一次回复好评（Good） |
| `Ctrl+B` | 给最后一次回复差评（Bad），触发反思 |
| `Ctrl+C` | 退出 |

## 命令

| 命令 | 作用 |
|------|------|
| `/quit` | 退出 |
| `/clear` | 清空当前会话和 Agent 记忆 |
| `/resume` | 列出历史会话，选择恢复 |
| `/help` | 显示帮助面板 |
| `/skills` | 列出所有技能（含状态、使用统计） |
| `/skills del <id>` | 按名称或 ID 前缀删除技能 |
| `/skills dep <id>` | 按名称或 ID 前缀废弃技能 |
| `/merge-skills` | LLM 驱动的技能去重合并 |
| `/reflect` | 手动触发技能反思 |
| `/cron [query]` | 查询当前 cron 任务列表，包含 durable 任务 |
| `:q` | 关闭覆盖面板（help/skills/sessions） |
| `/x` | 关闭 Toast 通知 |

## 用户反馈系统

- 使用技能后显示反馈提示：`👍 Ctrl+G Good  👎 Ctrl+B Bad  ⏎ skip`
- 好评：记录技能使用成功，提升置信度
- 差评：记录失败 + 标记为不满意，异步触发反思
- 跳过：直接输入下一条消息自动清除

## Toast 通知

- 屏幕顶部浮动通知条，2-4 秒自动消失
- 用于：反思结果、定时任务完成/失败、错误提示

## 状态栏

右侧 `#status-bar` 实时显示所有 Cron 后台任务：
- 图标：⟳ (运行中) / ⏱ (已调度)
- 显示任务名、状态、下次运行倒计时、是否 durable
- `next:` 倒计时按秒刷新；durable 任务在重启恢复后会立即出现在列表中，并重新绑定到当前会话

## 流式响应

### 用户 turn（async generator 路径）

```
AlexApp._run_chat()
  -> Agent.chat_stream()
  -> async for event:
       -> StreamRenderer.on_*()
       -> throttled UI update (~50ms)
  -> StreamRenderer.build_turn() -> bubble.finalize()
  -> NotificationController.show_feedback_prompt() (if skills used)
  -> ChatProjector.refresh_status_bar()
```

### Cron turn（event bus 路径）

```
CronManager fire
  -> TurnProcessor.run_cron_turn()
  -> bus.publish(ToolStarted) -> ChatProjector.on_cron_tool_started() -> StreamRenderer
  -> bus.publish(TokenEmitted) -> ChatProjector.on_cron_token() -> StreamRenderer
  -> bus.publish(CronDone) -> ChatProjector.on_cron_done() -> StreamRenderer -> finalize()
```

两者共用 `StreamRenderer` 管理 bubble 生命周期、token/thinking 收集、工具调用追踪和 turn 最终化。

## Markdown 渲染

`alex/tui/markdown.py` 提供 `render_response(text)` —— 输入纯文本、输出 Rich `Markdown` 渲染对象（或在禁用时直接回传字符串）。

| 路径 | 是否经过 Markdown |
|------|------------------|
| 用户 turn 流式 token（`StreamRenderer.on_token` → `bubble.set_response`） | 否（保持纯文本） |
| 流式过程中 thinking 提示 | 否（短，不必要） |
| `bubble.insert_tool` 把已生成文本提交为 prefix | **是** |
| `bubble.finalize` 重建 bubble 时的最终回复 | **是** |
| Cron turn 重建后的回复 | **是**（同样走 `finalize`） |
| ToolBubble 输出 / SystemBubble | 否（结构化、不应解析为 Markdown） |
| 用户 / 思考 / 技能块 | 否 |

只在 finalize 时切到 Markdown 是个权衡：
- 流式途中每个 token 都重新解析 Markdown 会让标题/列表反复重排，体验糟糕
- 终态需要正确渲染代码块、列表、加粗、内联代码，这是 LLM 输出最常见的格式

代码主题用 `ansi_dark`，让代码块继承终端调色板，不会出现刺眼的浅色背景。

通过环境变量 `ALEX_TUI_MARKDOWN=0` 或 `set_markdown_enabled(False)` 可整体关闭，回到纯文本渲染。

## 会话持久化

- 保存路径：`~/.alex/sessions/{session_id}.json`
- 每次启动默认新会话
- `/resume` 列出历史会话（时间 + 首条消息 + 轮次数）
- 恢复时还原 TUI 视图 + Agent Memory
- 持久化通过 `TurnCompleted` 事件自动触发（`SessionPersistence.subscribe(bus)`）

## 依赖

- `textual>=8.0.0` — TUI 框架
- `rich>=13.7.0` — 终端渲染（Textual 底层依赖）
- `langchain` + `langgraph` — Agent 框架和图执行引擎
- `APScheduler` — 后台定时任务调度

## 目录结构

```
alex/tui/
├── __init__.py
├── alex.tcss                   # CSS 样式表（156 行，从 app.py CSS_PATH 加载）
├── ports.py                    # _ControllerHost Protocol — TUI structural subtyping
├── app.py                      # AlexApp — Textual TUI 主类，wiring center
├── controller.py               # ChatControllerMixin — 命令、会话、toggles（343 行）
├── chat_projector.py           # ChatProjector — bus→widget 投影，cron renderers（含 _ProjectorHost Protocol）
├── notification_controller.py  # NotificationController — toast、feedback
├── view_state.py               # SessionViewState — UI 可变状态 dataclass
├── presenter.py                # AlexBubble / UserBubble / ToolBubble / SystemBubble
├── view_models.py              # ChatHistory / ChatTurn / _messages_to_turns
├── cron_history.py             # CronHistoryReadModel — 独立的 cron 历史读模型
├── tool_display.py             # 工具输出渲染组件
├── confirm_screen.py           # PermissionConfirmScreen — 权限确认 modal
├── markdown.py                 # render_response — Rich Markdown 渲染层
└── stream_renderer.py          # StreamRenderer — 用户/cron turn 共用渲染状态
```
