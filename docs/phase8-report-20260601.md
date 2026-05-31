# Alex 项目 Phase 8 改进报告

**日期**: 2026-06-01  
**分支**: `phase8-cronmanager-split-20260601`  
**版本**: v2.7 → v2.8

---

## 概述

Phase 8 完成了 `CronManager`（~558 行）的三件套拆分，将持久化、执行、调度三种职责解耦到独立组件。这是 `docs/refactor-modular-architecture.md` 中规划的最后一个核心重构阶段。

---

## 改动内容

### 1. CronStore — 持久化提取 (`alex/scheduler/cron_store.py`, ~90 行，新文件)

从 `CronManager` 提取 durable job 持久化逻辑：

- `persist(job)`: 原子写入 `~/.alex/cron/{job_id}.json`（tempfile + os.replace）
- `delete(job_id)`: 安全删除（检查文件存在性）
- `restore_all()`: 读取所有 `.json` 文件，自动清理已完成的一次性任务，将 RUNNING 状态重置为 SCHEDULED

**验证**: 12 个单元测试覆盖原子写、跳过非 durable、删除、恢复、完成的一次性任务跳过、RUNNING 状态重置、空目录、写入失败清理、Unicode 内容。

### 2. CronExecutor — 执行提取 (`alex/scheduler/cron_executor.py`, ~205 行，新文件)

从 `CronManager` 提取 runner 标准化和 execute-once 生命周期：

- `normalize_runner(runner)`: 静态方法，兼容旧式 5 参数和新式 `wait_until_done` runner
- `execute(job, runner, *, persist, delete_persisted, emit_job_event, debug, on_complete)`: 完整的执行生命周期
- `cancel_task(job_id)`: 取消运行中的任务
- 独立管理 `_session_locks`（session 互斥）和 `_running_tasks`（运行中任务注册）

**关键增强**:
- **`finalised` 守卫**: 防止外层 safety net 在 finalise 已经执行后重复递增 `runs_done`
- **Safety net 增强**: 外层 except 中的 `emit_job_event` 包裹 try/except，防止 emit 失败导致任务泄漏
- **`debug` 参数实际调用**: 添加 `executor_start` 日志

**验证**: 10 个单元测试覆盖成功路径、失败路径、recurring 任务保持 SCHEDULED、CancelledError 处理、预取消守卫、cancel_task、stream_id 格式、外层 safety net 防崩溃。

### 3. CronManager — 调度保留 (`alex/scheduler/manager.py`, ~435 行, -22%)

保留 APScheduler 生命周期管理和 schedule/cancel API，委托 CronStore 和 CronExecutor：

- 移除 `_persist_job`, `_delete_persisted_job`, `_job_path`, `_normalize_runner`
- 移除 `_session_locks`, `_running_tasks` 字典
- `_schedule_aps` 简化为创建 `_execute_job` 闭包并委托 `executor.execute()`
- 新增 `_on_job_complete(job_id)` 回调处理 APScheduler 清理
- `_cancel_inner` 使用 `executor.cancel_task(job_id)` 替代直接操作
- `restore_durable_jobs` 委托 `store.restore_all()` + `executor.normalize_runner()`

### 4. 其他改动

| 文件 | 改动 |
|------|------|
| `alex/scheduler/__init__.py` | 新增 `CronExecutor`, `CronStore` 导出 |
| `docs/refactor-modular-architecture.md` | Phase 8 标记完成，版本 v2.8，规划 Phase 9 |
| `docs/design.md` | scheduler 目录结构更新 |
| `README.md` | 测试数 319 → 325 |
| `tests/test_cron.py` | monkeypatch 路径更新 (`manager.os.replace` → `cron_store.os.replace`) |

---

## 架构改进

### 拆分前
```
CronManager (~558 行)
├── APScheduler 生命周期
├── schedule/cancel/list_jobs  (调度)
├── _run_once_async             (执行)
├── _normalize_runner           (标准化)
├── _persist_job / _delete      (持久化)
├── _session_locks              (并发控制)
└── _running_tasks              (任务跟踪)
```

### 拆分后
```
CronManager (~435 行)           CronExecutor (~205 行)          CronStore (~90 行)
├── APScheduler 生命周期        ├── normalize_runner()          ├── persist(job)
├── schedule/cancel/list_jobs   ├── execute()                   ├── delete(job_id)
├── _schedule_aps               ├── _session_locks              ├── restore_all()
├── _on_job_complete            ├── _running_tasks
├── restore_durable_jobs        └── cancel_task()
├── _emit_job_event
└── shutdown
```

### 关键指标

| 指标 | 之前 | 之后 | 变化 |
|------|------|------|------|
| CronManager 行数 | 558 | 435 | **-22%** |
| 组件数量 | 1 个大类 | 3 个聚焦类 | 职责分离 |
| CronStore 测试 | 0 (集成) | 12 (单元) | ✅ 新增 |
| CronExecutor 测试 | 0 (集成) | 10 (单元) | ✅ 新增 |
| 总测试数 | 303¹ | 325 | **+22** |
| 回归 | - | 0 | ✅ |

> ¹ 不含 test_agent.py 中因缺少 OPENAI_API_KEY 而 skip 的 11 个测试

---

## 架构约束验证

所有 [7 项必须保留的约束](docs/refactor-modular-architecture.md#必须保留的约束) 均保持不变：

1. ✅ DeepSeek thinking mode — 不受影响
2. ✅ `/help`、`/skills` modal 视图 — 不受影响
3. ✅ 后台任务绑定 Textual 事件循环 — 不受影响
4. ✅ Session 持久化保存 BaseMessage — 不受影响
5. ✅ Cron durable 任务落盘/恢复 — 经 CronStore 完整保留
6. ✅ `/resume` 恢复路径 — 不受影响
7. ✅ Turn 顺序一致性 — 不受影响

---

## Phase 9 规划（2026-06-02）

下一天任务（来自 `docs/refactor-modular-architecture.md`）：

1. **CronManager mock-based 单元测试**: 添加 `tests/test_cron_manager.py`，mock APScheduler `AsyncIOScheduler`，覆盖 schedule/cancel/list_jobs/restore_durable_jobs/shutdown 路径
2. **增量文档同步**: 更新 `docs/design.md`（scheduler 目录）、`docs/tools.md`（测试数）、`docs/display.md`（CronManager 行数）、`docs/roadmap-future-evolution.md`（版本基线 v2.7→v2.8）
3. **README.md**: 测试数 325 → 目标值

---

## 总结

Phase 8 成功将 Alex 从 v2.7 推进到 **v2.8**。CronManager 的三件套拆分是架构重构路线图的最后一项核心工作，完成后：

- 调度、执行、持久化三种职责解耦到独立类，可独立测试、独立演进
- CronStore 和 CronExecutor 各有专注的单元测试覆盖
- CronManager 精简 22%，仅保留 APScheduler 调度职责
- 所有 public API 保持向后兼容，CronService 和 Agent 无需改动
- 325 测试全部通过，0 回归
