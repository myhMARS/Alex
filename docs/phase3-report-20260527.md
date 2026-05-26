# Alex 项目改进报告 — Phase 3: Projection 与 UI 薄化

**日期**: 2026-05-27  
**分支**: `phase3-projection-ui-thinning-20260527`  
**提交**: `e3191e3` (code), `ccc2850` (docs)

---

## 执行摘要

Phase 3 成功将 TUI controller 从 608 行"超级控制器"拆分为 4 个职责清晰的独立对象，controller.py 降至 282 行（-54%），UI 状态变更路径统一为 `SessionViewState.reset()`。92/92 回归测试通过。

---

## 改动内容

### 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `alex/tui/view_state.py` | 28 | `SessionViewState` dataclass — 收口所有 UI 可变状态，`reset()` 统一入口 |
| `alex/tui/chat_projector.py` | 300 | `ChatProjector` — bus→widget 投影（11 个 event handler）、cron renderer 管理、status bar 刷新、cron history read model |
| `alex/tui/notification_controller.py` | 98 | `NotificationController` — toast 通知生命周期、feedback prompt 管理、rating 提交 |

### 修改文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `alex/tui/controller.py` | 608→282 (-54%) | 仅保留命令分发、page 管理、session 生命周期、toggles |
| `alex/tui/app.py` | +16 lines | 从状态容器升级为 wiring center：装配 projector/notifications/view_state |
| `alex/tui/__init__.py` | +6 lines | 导出新组件 |
| `tests/test_tui.py` | 1 line | 更新 bus subscribe 引用 |
| `docs/design.md` | 更新 | 项目结构、tui 文件列表 |
| `docs/display.md` | 更新 | 核心组件表、运行时流程图、目录结构 |
| `docs/refactor-modular-architecture.md` | 更新 | Phase 3 完成标记、Phase 4 规划 |

---

## 架构改进

### Before (Phase 2)
```
ChatControllerMixin (608 lines)
  ├── bus event handlers (cron/skill stream ×11)
  ├── feedback (prompt, dismiss, rate)
  ├── toast (show, dismiss, format)
  ├── cron history (format, persist)
  ├── status bar refresh
  ├── commands (help, skills, cron, session)
  ├── session lifecycle (resume, clear, merge)
  └── toggles (thinking, skills)
```

### After (Phase 3)
```
AlexApp (wiring center, 442 lines)
  ├── ChatProjector (300 lines)
  │     ├── bus→widget event handlers
  │     ├── cron renderer management
  │     ├── status bar + cron history
  │     └── trim_chat_view helper
  ├── NotificationController (98 lines)
  │     ├── toast lifecycle
  │     └── feedback prompt + rating
  ├── SessionViewState (28 lines)
  │     └── reset() on session switch
  └── ChatControllerMixin (282 lines)
        ├── commands dispatch
        ├── page management
        ├── session lifecycle
        └── toggles
```

### 关键架构收益
1. **单一职责**: 每个类只做一件事，不再横跨命令/投影/状态/通知
2. **统一 reset**: `SessionViewState.reset()` 是 session 切换时的唯一状态重置入口
3. **可测试性**: projector / notifications / view_state 可独立打桩测试
4. **controller 缩减 54%**: 从 608 行降至 282 行

---

## 测试结果

```
92 passed in 5.32s (100%)
- test_bus: 10 passed
- test_cron: 1 passed
- test_crontab: 1 passed
- test_feedback: 11 passed
- test_memory: 17 passed
- test_orchestrator: 13 passed
- test_store: 9 passed
- test_time_tool: 2 passed
- test_tools: 6 passed
- test_tools_registry: 9 passed
- test_tui: 13 passed
```

---

## 下一天任务 (Phase 4: Runtime 与状态模型收口)

计划日期: **2026-05-28**

1. **引入 `ToolExecutionContext`** 为一等运行时上下文
   - `tools/ports.py` 中已定义 `ToolExecutionContext` dataclass
   - 需集成到 `ToolExecutor.execute(ctx, name, args)` 签名

2. **独立 `CronHistoryReadModel`**
   - 从 `ChatHistory` 中分离 cron_history 为独立 read model

3. **修复 `SessionService` 边界泄露**
   - 不再直接 import `store.session.deserialize_message`
   - 引入 `SessionSerializer` 中间层

4. **Agent wiring 工厂化**
   - 从手工组合改为显式 factory 函数

5. **`push_notification()` 语义收口**
   - 消除 "publish + create_task 旁路处理" 混合模式
