# TUI 交互界面 (`alex/tui.py`)

## 设计思路

基于 **Textual** 框架构建终端 TUI 应用，运行在 alternate screen buffer 中。采用组件化架构，所有对话内容作为 widget 挂载到可滚动容器中，通过 CSS class 切换实现折叠/展开，避免 DOM 重建导致的页面跳动。

## 架构

```
┌─────────────────────────────────────────────────────┐
│  Header (标题 + 快捷键提示)                          │
├─────────────────────────────────────────────────────┤
│  VerticalScroll #chat-view                          │
│  ┌───────────────────────────────────────────────┐  │
│  │ UserBubble (cyan border)                      │  │
│  ├───────────────────────────────────────────────┤  │
│  │ AlexBubble (green border)                     │  │
│  │  ├─ tools-collapsed / tools-expanded          │  │
│  │  ├─ thinking-collapsed / thinking-expanded    │  │
│  │  └─ response-text (Markdown)                  │  │
│  ├───────────────────────────────────────────────┤  │
│  │ UserBubble                                    │  │
│  ├───────────────────────────────────────────────┤  │
│  │ AlexBubble                                    │  │
│  └───────────────────────────────────────────────┘  │
├─────────────────────────────────────────────────────┤
│  Input #input-box                                   │
└─────────────────────────────────────────────────────┘
```

## 核心组件

| 组件 | 职责 |
|------|------|
| `AlexApp` | Textual App 主类，管理状态和事件 |
| `UserBubble` | 用户消息气泡（cyan 圆角边框） |
| `AlexBubble` | AI 回复容器（green 圆角边框），内含 tools/thinking/response |
| `ChatHistory` | 会话持久化，保存到 `~/.alex/sessions/` |
| `ChatTurn` | 单轮对话数据模型 |

## 折叠/展开机制

采用 **CSS `display: none` 切换**，不销毁/重建 DOM：

- `AlexBubble` 在 `compose()` 时同时生成 expanded 和 collapsed 两个版本
- 通过 `.hidden` CSS class（`display: none`）控制哪个可见
- `set_thinking_expanded()` / `set_tools_expanded()` 只切换 class，不触发布局重建
- 页面不会因为切换而产生滚动跳动

```python
# 切换时只操作 CSS class
def set_thinking_expanded(self, expanded):
    for w in self.query(".thinking-expanded"):
        w.set_class(not expanded, "hidden")
    for w in self.query(".thinking-collapsed"):
        w.set_class(expanded, "hidden")
```

## 快捷键

| 快捷键 | 作用 |
|--------|------|
| `Ctrl+T` | 切换所有 thinking 展开/收起 |
| `Ctrl+D` | 切换所有工具调用展开/收起 |
| `Ctrl+C` | 退出 |

## 命令

| 命令 | 作用 |
|------|------|
| `/quit` | 退出 |
| `/clear` | 清空当前会话 |
| `/resume` | 显示历史会话列表，选择恢复 |

## 会话持久化

- 保存路径：`~/.alex/sessions/{timestamp}.json`
- 每次启动默认新会话
- `/resume` 列出历史会话（时间 + 首条消息前 20 字符 + 轮次数）
- 恢复时同时还原 Agent 对话记忆

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
      "tool_calls": []
    }
  ]
}
```

## 显示效果

### 收起状态

```
╭─ Alex ──────────────────────────────────────────────╮
│ � 3 tool calls: web_search ×2, web_fetch [Ctrl+D] │
│ �💭 Thinking (134 chars) [Ctrl+T]                    │
│                                                      │
│ 你好！我是 Alex，一个有帮助的 AI 助手...             │
╰──────────────────────────────────────────────────────╯
```

### 展开状态

```
╭─ Alex ──────────────────────────────────────────────╮
│ ◈ web_search                                        │
│   ├─ query: 杭州到上海高铁 2025年 明天 票价          │
│   └─ ✓ Search results for: '杭州到上海高铁...'      │
│ ◈ web_fetch                                         │
│   ├─ url: https://example.com/...                   │
│   └─ ✓ Page content fetched                         │
│ ╭─ 💭 Thinking ─────────────────────────────────╮   │
│ │ 用户想查高铁票价，我需要搜索最新信息...        │   │
│ ╰────────────────────────────────────────────────╯   │
│                                                      │
│ 根据搜索结果，杭州到上海的高铁票价...                │
╰──────────────────────────────────────────────────────╯
```

## 非 TUI 模式

`main.py` 也支持非 TUI 的简单 CLI 模式：

- `python main.py "query"` — 单次查询，Rich 输出
- `python main.py --stream "query"` — 流式输出，Rich Live

这些模式使用 `alex/display.py` 中的 Rich Console 工具函数。

## 依赖

- `textual>=8.0.0` — TUI 框架（alternate screen、组件化、CSS 样式）
- `rich>=13.7.0` — 终端渲染（Markdown、Panel，Textual 底层依赖）

## 未来演进

- 多窗格布局（左侧会话列表 + 右侧对话）
- 内联图片预览（iTerm2/Kitty 协议）
- 文件拖放上传
- 自定义主题/配色
