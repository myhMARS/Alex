# TUI 交互界面 (`alex/tui.py`)

## 设计思路

基于 **Textual** 框架构建终端 TUI 应用，运行在 alternate screen buffer 中。采用组件化架构，所有对话内容作为 widget 挂载到可滚动容器中，通过 CSS class 切换实现折叠/展开，避免 DOM 重建导致的页面跳动。

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

| 组件 | 职责 |
|------|------|
| `AlexApp` | Textual App 主类，管理状态、事件、通知轮询 |
| `UserBubble` | 用户消息气泡（cyan 圆角边框） |
| `AlexBubble` | AI 回复容器（green 圆角边框），内含 skills/tools/thinking/response |
| `ToolBubble` | 单个工具调用展示（实线边框，含参数和结果） |
| `SystemBubble` | 系统通知消息（反思结果等） |
| `ChatHistory` | 会话持久化，保存到 `~/.alex/sessions/` |
| `ChatTurn` | 单轮对话数据模型（含 skills 字段） |

## 折叠/展开机制

采用 **CSS `display: none` 切换**，不销毁/重建 DOM：

- `AlexBubble` 在 `compose()` / `finalize()` 时同时生成 expanded 和 collapsed 两个版本
- 通过 `.hidden` CSS class（`display: none`）控制哪个可见
- `set_thinking_expanded()` / `set_skills_expanded()` 只切换 class，不触发布局重建
- 页面不会因为切换而产生滚动跳动

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
- 图标：⟳ (运行中) / ⏱ (已调度) / ✓ (成功) / ✗ (失败) / ⦸ (已取消)
- 显示任务名、状态、下次运行倒计时、已完成次数
- 每 100ms 轮询刷新

## Cron 流式响应

当 cron job 设置 `subscribe=true` 时，执行结果以完整对话气泡形式注入聊天视图：
- 创建 `AlexBubble`，实时流式渲染工具调用和 AI 回复
- 支持 tool_start/tool_end/token/thinking 事件流

## 会话持久化

- 保存路径：`~/.alex/sessions/{timestamp}.json`
- 每次启动默认新会话
- `/resume` 列出历史会话（时间 + 首条消息前 20 字符 + 轮次数）
- 恢复时还原 TUI 视图 + Agent Memory

```json
{
  "session_id": "20250514_170327",
  "created_at": "2025-05-14T17:03:27",
  "first_message": "你好",
  "turns": [
    {
      "user_input": "你好",
      "response": "你好！我是 Alex...",
      "thinking": "用户用中文打招呼...",
      "tool_calls": [],
      "skills": []
    }
  ]
}
```

## 非 TUI 模式

`main.py` 也支持非 TUI 的简单 CLI 模式：

- `python main.py "query"` — 单次查询，Rich 输出（含 thinking panel）
- `python main.py --stream "query"` — 流式输出，Rich Live

这些模式使用 `alex/display.py` 中的 Rich Console 工具函数和 `ThinkingDisplay`。

## 依赖

- `textual>=8.0.0` — TUI 框架（alternate screen、组件化、CSS 样式）
- `rich>=13.7.0` — 终端渲染（Markdown、Panel，Textual 底层依赖）
