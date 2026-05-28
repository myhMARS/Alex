# Alex Phase 5 改进报告 — 2026-05-29

## 迭代概览

**分支**: `phase5-adapter-tests-20260529`  
**基准**: `master` (a7b46c8)  
**测试**: 279 passed, 5 skipped (+34 新增)  
**架构版本**: v2.3 → v2.4

---

## 一、SkillManager 兼容层移除

### 改了什么

从 12 个源文件中移除 `SkillManager` 类及其所有引用，统一使用 `SkillService`：

| 文件 | 改动 |
|------|------|
| `alex/skill/models.py` | 移除 `SkillManager` 类（35 行），保留 `Skill` 数据类 |
| `alex/skill/__init__.py` | 移除 `SkillManager` 导出 |
| `alex/agent/factory.py` | 替换为 `_create_default_skill_service()` 工厂函数 |
| `alex/agent/service.py` | 同上 |
| `alex/agent/chat_service.py` | 类型注解 `SkillManager` → `SkillService` |
| `alex/agent/orchestrator.py` | 同上 |
| `alex/agent/prompt.py` | 同上 |
| `alex/agent/feedback_service.py` | 同上 |
| `alex/agent/skill_admin_service.py` | 同上 |
| `alex/agent/feedback.py` | 同上（legacy） |
| `alex/skill/ports.py` | 更新文档注释 |

### 为什么

`SkillManager` 曾是 `SkillService` 的子类，仅在构造函数中提供 lazy default 依赖构造。这个兼容层没有提供额外业务逻辑，反而增加了 import 路径的认知负担。移除后每个调用点都显式知道自己在使用 `SkillService`。

---

## 二、SkillStore 原子写强化

### 改了什么

`alex/skill/repository.py` 完全重写 `_save()` 和 `_load()`：

**原子写**：
```python
fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
os.write(fd, payload.encode("utf-8"))
os.close(fd)
os.replace(tmp, self._path)  # 原子替换
```

**6 层 corrupt data 防御**：
1. 文件不存在 → 安全返回空
2. `OSError` 读取 → 警告 + 空启动
3. 空文件 → 安全返回空
4. JSON 解析错误 → 警告 + 空启动
5. 顶层非数组 → 警告 + 空启动
6. 单个条目无效 → 跳过 + 警告

---

## 三、新增测试

### 3.1 Port Contract Tests (`tests/test_port_contracts.py` — 13 tests)

| 测试类 | 覆盖内容 |
|--------|---------|
| `TestSessionRepositoryContract` | save/load roundtrip, delete, cron append, list, nonexistent |
| `TestSkillServicePortContract` | CRUD, retrieval by name/id, deprecation, usage recording, prompt injection |
| `TestSkillStoreAtomicWrite` | corrupt JSON → empty, empty file → empty, atomic write integrity |

### 3.2 State Model Tests (`tests/test_state_models.py` — 7 tests)

| 测试类 | 覆盖内容 |
|--------|---------|
| `TestFeedbackSessionState` | per-session isolation, session switch reset, episode append, reflect trigger on interval |
| `TestCronCancel` | nonexistent job cancel returns False, existing job cancel → CANCELLED status |
| `TestSessionViewState` | reset() correctness, default values |

### 3.3 Event Bus Serial Semantics (`tests/test_event_bus_semantics.py` — 8 tests)

| 测试类 | 覆盖内容 |
|--------|---------|
| `TestSerialDispatch` | same-session events ordered, different-session events dispatched |
| `TestHandlerIsolation` | failing handler doesn't block other handlers |
| `TestBufferedPublish` | pre-start events drained on start |
| `TestSubscribeUnsubscribe` | unsubscribe stops delivery, unsubscribe nonexistent is noop |
| `TestCrossThreadPublish` | thread-safe publish |
| `TestIsinstanceMatching` | base-type subscribers receive subclass events |

---

## 四、文档更新

- `docs/refactor-modular-architecture.md` — 更新至 v2.4，新增 Phase 5 里程碑、差距表、Phase 6 规划
- `docs/skills.md` — 移除 SkillManager 引用，更新架构图
- `docs/phase5-report-20260528.md` — 本报告

---

## 五、Phase 6 规划（2026-05-30）

1. **Cron/Scheduler 统一抽象** — `CronAppService + SchedulerAdapter + CronExecutionCoordinator` 三层
2. **Read Model 显式化** — `SessionListReadModel`、`FeedbackReadModel` 独立 dataclass
3. **ChatHistory 职责收缩** — 移除 cron_history / session list 字段
4. **CronExecutionCoordinator contract tests**
5. **DI container 调查** — 评估 `punq` / `dependency-injector`

---

## 六、测试统计

| 类型 | 数量 |
|------|------|
| Port contract tests | 13 |
| State model tests | 7 |
| Event bus semantics tests | 8 |
| 已有功能测试 | 245 |
| 通过 | 279 |
| 跳过（需 API key） | 5 |

---

## 七、一句话总结

Alex 架构从 v2.3 升级至 v2.4：SkillManager 已完全退出历史舞台，SkillStore 具备原子写和全面的 corrupt data 容错，34 个新测试覆盖了 port contract、state model 和 event bus 语义。下一阶段将统一 Cron/Scheduler 抽象并显式化 Read Model 层。
