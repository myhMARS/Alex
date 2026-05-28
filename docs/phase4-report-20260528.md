# Alex 项目改进报告 — Phase 4: Runtime 与状态模型收口

**日期**: 2026-05-28  
**分支**: `phase4-runtime-state-model-20260528`  
**提交**: `34a4b22`

---

## 执行摘要

Phase 4 完成了 5 项 runtime 和状态模型改进：SessionSerializer 消除 store 边界泄露、CronHistoryReadModel 独立、Agent wiring 工厂化、push_notification 语义收口、ToolExecutionContext 确认为一等对象。92/92 回归测试通过。

---

## 改动内容

### 新增文件

| 文件 | 行数 | 职责 |
|------|------|------|
| `alex/store/session_serializer.py` | 23 | BaseMessage ↔ dict roundtrip 中间层，agent 层安全入口 |
| `alex/tui/cron_history.py` | 59 | `CronHistoryReadModel` — 独立 cron 历史读模型 |
| `alex/agent/factory.py` | 58 | `create_agent()` — 显式 wiring factory 函数 |

### 修改文件

| 文件 | 改动 | 说明 |
|------|------|------|
| `alex/agent/session_service.py` | 1 line | import 从 `store.session` 改为 `store.session_serializer` |
| `alex/agent/chat_service.py` | 13 lines | `push_notification` 简化为纯 bus 发布；新增 `dispatch_cron_reply` |
| `alex/agent/service.py` | 15 lines | `push_notification` 语义收口：统一 publish + 显式 cron dispatch |
| `alex/tui/view_models.py` | 17 lines | `ChatHistory` 委托 `CronHistoryReadModel` 管理 cron 历史 |
| `alex/tui/__init__.py` | 2 lines | 导出 `CronHistoryReadModel` |
| `docs/refactor-modular-architecture.md` | 73 lines | Phase 4 完成标记，Phase 5 规划 |

---

## 架构改进

### 1. SessionSerializer — 消除边界泄露
```
Before: SessionService → import deserialize_message from store.session
After:  SessionService → import deserialize_message from store.session_serializer
                                                              └─ re-exports from store.session
```
效果：agent 层不再直接依赖 `store.session` 内部实现。

### 2. CronHistoryReadModel — 独立读模型
```
Before: ChatHistory._cron_history: list[dict]  (内联)
After:  ChatHistory._cron: CronHistoryReadModel (委托)
        CronHistoryReadModel
          ├── records, add(record), clear(), restore(records)
          └── query(q, limit) → filtered records
```
效果：cron_history 不再是 ChatHistory 的附属字段。

### 3. Agent wiring 工厂化
```
Before: caller = Agent(llm=..., memory=..., skill_manager=..., ...)
After:  caller = create_agent(llm=..., ...)  # 自动填充默认值
```
效果：默认 memory/skills/llm 不再散落在 main.py 中。

### 4. push_notification 语义收口
```
Before:
  Agent.push_notification(event)
    ├─ CronJobEvent+subscribe → chat.push_notification → bus.publish + create_task
    └─ else → bus.publish

After:
  Agent.push_notification(event)
    ├─ bus.publish(event)  ← 始终执行
    └─ CronJobEvent+subscribe → chat.dispatch_cron_reply(event) ← 显式分离
```
效果：单一发布路径，不再有 "publish + create_task 旁路处理" 混合模式。

---

## 测试结果

```
92 passed in 5.16s (100%)
```

---

## 下一天任务 (Phase 5: Adapter 强化与测试治理)

计划日期: **2026-05-29**

1. **移除 SkillManager 兼容层** — 从 agent/service.py 和 chat_service.py 移除 `SkillManager` import
2. **增加 port contract tests** — `SessionRepository`、`SkillServicePort`
3. **增加 state model tests** — session 切换、feedback state、cron cancel 语义
4. **增加 event bus 串行语义 tests**
5. **强化 SkillStore 原子写与坏数据处理**
