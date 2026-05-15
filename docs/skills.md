# 自适应技能系统 (`alex/skills/`)

## 设计目标

Agent 具备成长性：从历史对话中识别模式、提炼策略性技能、自主管理技能生命周期，在后续对话中复用已验证的技能提升响应质量。

**关键原则**：技能存储的是"策略"（HOW to respond），不是"知识"（WHAT is the fact）。

## 核心数据模型：Skill

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识 |
| `name` | 技能名称 |
| `pattern` | 触发场景描述（什么情况下使用） |
| `instruction` | 响应策略（具体怎么做） |
| `tags` | 标签列表，用于检索匹配 |
| `examples` | 示例场景 |
| `status` | 生命周期状态：`CANDIDATE` / `ACTIVE` / `DEPRECATED` |
| `use_count` | 被使用次数 |
| `success_count` / `failure_count` | 效果追踪 |
| `confidence` | 置信度（基于使用次数 + 成功率的贝叶斯估计） |
| `version` | 版本号，每次更新递增 |

## 组件职责

| 组件 | 职责 |
|------|------|
| **SkillManager** | 对外统一接口，协调下面各组件 |
| **Reflector** | 反思引擎 — 调用 LLM 分析对话，提炼新技能或给出更新建议 |
| **SkillRetriever** | 检索器 — 根据当前 query 匹配最相关的活跃技能 |
| **EvolutionEngine** | 进化引擎 — 评估技能效果，执行晋升/废弃/合并 |
| **SkillStore** | 持久化 — 技能的存取（JSON/SQLite，可替换） |

## 核心业务流程

### 对话时：技能检索 & 注入

```
用户输入
  │
  ▼
SkillRetriever.retrieve(query, top_k=3)
  │  基于标签匹配 + pattern 关键词 + 置信度加权
  │  (未来可升级为 embedding 语义检索)
  │
  ▼
将匹配到的技能格式化为提示文本
  │
  ▼
动态追加到 system prompt 末尾
  │
  ▼
正常执行 Agent 对话流程
  │
  ▼
记录本次使用了哪些 skill_id（用于后续反馈追踪）
```

### 对话后：反思 & 提炼

```
判断是否触发反思（见触发策略）
  │
  ▼ (是)
从 Memory 获取近期对话（最近 N 条）
  │
  ▼
Reflector 调用 LLM 分析对话
  │  Prompt 引导 LLM 判断：
  │  - 是否存在可复用的响应模式？
  │  - 该模式的触发场景是什么？
  │  - 具体的响应策略是什么？
  │  - 是否与已有技能重复？
  │
  ▼
输出 ReflectionResult:
  ├─ new_skills: 新提炼的技能 (status=CANDIDATE)
  ├─ updated_skills: 需要更新的已有技能
  └─ deprecated_skills: 建议废弃的技能
  │
  ▼
SkillStore 持久化变更
  │
  ▼
EvolutionEngine.evolve() — 执行生命周期评估
```

### 用户反馈：效果追踪

```
用户给出正/负反馈（显式或隐式）
  │
  ▼
SkillManager.record_usage(skill_id, success=True/False)
  │
  ▼
更新 skill 的 use_count / success_count / failure_count
  │
  ▼
置信度自动重算（贝叶斯平滑）
```

## 技能生命周期

```
  反思提炼             验证通过              效果衰退
───────────►  CANDIDATE  ───────────►  ACTIVE  ───────────►  DEPRECATED
                 │                       │  ▲
                 │ 验证失败                │  │ 反思更新
                 ▼                       └──┘
             DEPRECATED
```

| 转换 | 条件 |
|------|------|
| CANDIDATE → ACTIVE | `use_count >= 3` 且 `success_rate >= 0.7` |
| CANDIDATE → DEPRECATED | `use_count >= 5` 且 `success_rate < 0.3` |
| ACTIVE → DEPRECATED | `use_count >= 5` 且 `success_rate < 0.3`（持续表现差） |
| ACTIVE → ACTIVE (更新) | 反思发现需要修正 instruction，version +1 |

## 反思触发策略

| 触发条件 | 说明 |
|---------|------|
| 定期反思 | 每 N 轮对话自动触发（默认 N=5） |
| 负反馈触发 | 用户明确表示不满意时立即反思 |
| 新领域检测 | 当前 query 无任何技能匹配时，标记为新领域，对话结束后反思 |
| 手动触发 | 用户输入 `/reflect` 命令 |

## 安全约束

- **技能上限**：活跃技能数量限制（默认 50），超出时按置信度淘汰最弱的
- **幻觉防护**：技能 instruction 只存策略指导，不存具体事实
- **版本控制**：每次更新递增 version，可回溯
- **人工审核（可选）**：CANDIDATE → ACTIVE 可配置为需要用户确认

## 目录结构

```
alex/skills/
├── __init__.py
├── base.py           # Skill 数据模型 & SkillManager 接口
├── store.py          # 技能持久化存储
├── reflector.py      # 反思引擎
├── retriever.py      # 技能检索匹配
└── evolution.py      # 进化策略 & 生命周期
```
