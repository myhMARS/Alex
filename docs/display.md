# TUI 交互界面 (`alex/tui/`)

## 设计思路

基于 **Textual** 框架构建终端 TUI 应用，运行在 alternate screen buffer 中。采用组件化架构，所有对话内容作为 widget 挂载到可滚动容器中，通过 CSS class 切换实现折叠/展开。

`TuiModule` 是 TUI 的 bus 入口，负责：
- 发布 `UserTurnRequested` Command（用户输入时）
- 订阅所有 UI 相关事件（`TokenEmitted`, `ThinkingUpdated`, `ToolStarted` 等）
- 路由事件到 `AlexApp` 进行渲染

## 架构布局

```
┌────────────────────────────────────────────────────────────┐
│  Header (标题 + 时钟)                                       │
├───────────────────────────────────────┬────────────────────┤
│  VerticalScroll #chat-view            │ VerticalScroll     │
│  ┌────────────────────────────────┐   │ #status-bar        │
│  │ UserBubble (cyan border)       │   │ ┌──────────────┐   │
│  ├────────────────────────────────┤   │ │ 定时任务       │   │
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

## TuiModule

`module.py` 是 TUI 的 bus 入口。与 `AlexApp` 的关系：

- `ModuleHost` 启动所有后台模块后，`entry.py` 创建 `AlexApp(bus)` 并调用 `run_async()`
- `TuiModule` 在模块注册列表中存在但由 `entry.py` 跳过（TUI runs last）
- `AlexApp` 内部持有 `TuiModule` 实例，用于 `publish_user_turn()` 等 bus 通信

## 核心组件

| 组件 | 文件 | 职责 |
|------|------|------|
| `TuiModule` | `module.py` | bus 入口：发布 UserTurnRequested，路由 UI 事件到 app |
| `AlexApp` | `app.py` | Textual App 主类，wiring center — 装配 projector/notifications/view_state |
| `ChatControllerMixin` | `controller.py` | 命令分发、page 管理、session 生命周期、toggles |
| `ChatProjector` | `chat_projector.py` | bus→widget 事件投影、cron renderer 管理、status bar |
| `_ControllerHost` | `ports.py` | TUI structural subtyping Protocol |
| `NotificationController` | `notification_controller.py` | toast 通知、feedback prompt、rating 提交 |
| `SessionViewState` | `view_state.py` | UI 可变状态 dataclass |
| `StreamRenderer` | `stream_renderer.py` | 共享流式渲染状态管理（用户/cron turn 共用） |
| `UserBubble` | `presenter.py` | 用户消息气泡（cyan 圆角边框） |
| `AlexBubble` | `presenter.py` | AI 回复容器（green 圆角边框） |
| `ToolBubble` | `presenter.py` | 单个工具调用展示 |
| `SystemBubble` | `presenter.py` | 系统通知消息 |
| `ChatHistory` | `view_models.py` | 会话视图模型，维护 ChatTurn 列表 |
| `ChatTurn` | `view_models.py` | 单轮对话数据模型 |

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
| `/cron [query]` | 查询当前 cron 任务列表 |
| `:q` | 关闭覆盖面板（help/skills/sessions） |
| `/x` | 关闭 Toast 通知 |

## 用户反馈系统

- 使用技能后显示反馈提示：`👍 Ctrl+G Good  👎 Ctrl+B Bad  ⏎ skip`
- 好评：发布 `FeedbackSubmitted(positive=True)` → SkillModule 记录 skill usage
- 差评：发布 `FeedbackSubmitted(positive=False)` → SkillModule 触发反思
- 跳过：直接输入下一条消息自动清除

## Toast 通知

- 屏幕顶部浮动通知条，2-4 秒自动消失
- 用于：反思结果（`SkillsReflected`）、Cron 任务完成/失败（`CronJobEvent`）、错误提示

## 状态栏

右侧 `#status-bar` 实时显示所有 Cron 定时任务：
- 图标：⟳ (运行中) / ⏱ (已调度)
- 显示任务名、状态、下次运行倒计时、是否 durable
- `next:` 倒计时按秒刷新

## 流式响应

### 用户 turn（bus 路径）

```
TuiModule.publish_user_turn() → bus.publish(UserTurnRequested)
  → AgentModule._on_user_turn()
    → Agent.chat_stream()
      → bus.publish(TokenEmitted) → TuiModule._route_to_app() → StreamRenderer
      → bus.publish(ToolStarted)  → TuiModule._route_to_app() → ChatProjector
      → bus.publish(ToolFinished) → TuiModule._route_to_app() → ChatProjector
      → bus.publish(TurnCompleted) → TuiModule + StoreModule
```

### Cron turn（bus 路径）

```
CronModule → bus.publish(CronTurnRequested)
  → AgentModule._on_cron_turn()
    → bus.publish(CronJobEvent) → TUI status bar 更新
    → bus.publish(TokenEmitted) → TuiModule._route_to_app() → StreamRenderer
    → bus.publish(CronDone)     → TuiModule._route_to_app() → finalize
```

两者共用 `StreamRenderer` 管理 bubble 生命周期、token/thinking 收集、工具调用追踪和 turn 最终化。

## Markdown 渲染

`alex/tui/markdown.py` 提供 `render_response(text)` —— 输入纯文本、输出 Rich `Markdown` 渲染对象。

| 路径 | 是否经过 Markdown |
|------|------------------|
| 用户 turn 流式 token | 否（保持纯文本） |
| `bubble.finalize` 重建 bubble 时 | **是** |
| Cron turn 重建后的回复 | **是** |
| ToolBubble 输出 / SystemBubble | 否 |

代码主题用 `ansi_dark`，让代码块继承终端调色板。通过 `ALEX_TUI_MARKDOWN=0` 可关闭。

## 会话持久化

- 保存路径：`~/.alex/sessions/{session_id}.json`
- `StoreModule` 订阅 `TurnCompleted` 事件自动保存
- `/resume` 列出历史会话，恢复时还原 TUI 视图 + Agent Memory

## 目录结构

```
alex/tui/
├── __init__.py
├── module.py                   # TuiModule — bus 入口
├── alex.tcss                   # CSS 样式表
├── ports.py                    # _ControllerHost Protocol
├── app.py                      # AlexApp — Textual TUI 主类
├── controller.py               # ChatControllerMixin — 命令、会话、toggles
├── chat_projector.py           # ChatProjector — bus→widget 投影
├── notification_controller.py  # NotificationController — toast、feedback
├── view_state.py               # SessionViewState — UI 可变状态
├── presenter.py                # AlexBubble / UserBubble / ToolBubble / SystemBubble
├── view_models.py              # ChatHistory / ChatTurn
├── cron_history.py             # CronHistoryReadModel
├── tool_display.py             # 工具输出渲染组件
├── confirm_screen.py           # PermissionConfirmScreen — 权限确认 modal
├── markdown.py                 # render_response — Rich Markdown 渲染层
└── stream_renderer.py          # StreamRenderer — 共享渲染状态
```
