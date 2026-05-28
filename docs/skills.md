# 自适应技能系统 (`alex/skill/`)

## 设计目标

Agent 具备成长性：从历史对话中识别模式、提炼策略性技能、自主管理技能生命周期，在后续对话中复用已验证的技能提升响应质量。

**关键原则**：技能存储的是"策略"（HOW to respond），不是"知识"（WHAT is the fact）。

## 核心数据模型：Skill

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识（12 位 hex） |
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

## 架构分层

```
SkillService（构造函数注入全部依赖）
    ├── SkillStore      # JSON 文件持久化 + Jinja2 模板管理
    ├── SkillRetriever  # 标签 + 关键词 + 置信度加权检索
    ├── Reflector       # LLM JSON-mode 反思引擎
    └── EvolutionEngine # 生命周期进化状态机
```

## 组件职责

| 组件 | 文件 | 职责 |
|------|------|------|
| **SkillService** | `service.py` | 业务逻辑编排：检索、反思、合并、CRUD |
| **Skill** | `models.py` | 纯数据类，不含业务逻辑 |
| **SkillStore** | `repository.py` | JSON 文件持久化 + `~/.alex/skills/prompts/` 模板管理 |
| **SkillRetriever** | `matcher.py` | 标签匹配 + 关键词 + 置信度加权，返回 top-K |
| **Reflector** | `reflector.py` | LLM 反思引擎 — 分析对话 + episodes，返回 `ReflectionResult` |
| **EvolutionEngine** | `evolution.py` | 生命周期状态机 — CANDIDATE → ACTIVE → DEPRECATED |

## 核心业务流程

### 对话时：技能目录注入 + 按需加载

```
用户输入
  │
  ▼
SkillService.inject_skills_prompt(query)
  │  渲染 skills_section.j2 模板
  │  列出所有活跃技能的名称 + pattern（轻量目录）
  │
  ▼
追加到 system prompt（仅在技能列表变化时重建 graph）
  │
  ▼
Agent 执行对话流程
  │  当 Agent 判断某技能匹配时：
  │  调用 load_skill(skill_name) 工具
  │  → 加载该技能的完整 instruction
  │  → TUI 显示 SkillLoaded 事件
  │
  ▼
记录本轮的 loaded_skill_ids（用于反馈追踪）
```

### 对话后：反思 & 提炼

```
判断是否触发反思（见触发策略）
  │
  ▼ (是)
从 Memory 获取近期对话（最近 20 条）+ 积累的 episodes
  │
  ▼
Reflector 调用 LLM（JSON mode）分析
  │  输入：对话消息 + 已有技能列表 + problem-solving episodes
  │  输出：ReflectionResult
  │
  ▼
ReflectionResult:
  ├─ new_skills: 新提炼的技能 (status=CANDIDATE)
  ├─ updated_skills: 需要更新的已有技能
  └─ deprecated_ids: 建议废弃的技能
  │
  ▼
SkillStore 持久化变更 + 更新 Jinja2 模板
  │
  ▼
EvolutionEngine.evolve() — 执行生命周期评估
```

### 多轮 Episode 采集

Agent 每轮对话记录一个 episode：
```python
{
    "query": "用户问题（前 200 字符）",
    "skills_loaded": ["已加载技能名"],
    "tools_used": ["web_search", "web_fetch"],
    "outcome": "回复摘要（前 300 字符）"
}
```
Episodes 在反思时传递给 Reflector，帮助 LLM 理解完整的问题解决过程。

### 用户反馈：效果追踪

```
用户按 Ctrl+G / Ctrl+B
  │
  ▼
Agent.provide_feedback(positive, turn_id)
  │
  ▼
SkillService.record_usage(skill_id, success=True/False)
  │
  ▼
更新 use_count / success_count / failure_count → 置信度自动重算
  │
  ▼ (负反馈)
异步触发反思
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

## 技能上限

活跃技能数量默认限制 50 个。超出时按置信度升序淘汰最低分技能 → DEPRECATED。

## LLM 驱动的技能合并（`/merge-skills`）

- 将所有活跃技能提交给 LLM，识别语义重复或高度相似的技能
- LLM 返回 `merged_groups`（保留 + 合并列表）和 `deprecate_ids`
- 合并时更新 keeper 的 name/pattern/instruction/tags，删除冗余技能
- 用于定期清理技能库，防止膨胀

## 技能模板系统

- 每个技能自动生成 `~/.alex/skills/prompts/{skill_id}.j2` Jinja2 模板
- `load_skill` 工具返回渲染后的完整技能卡
- 新增/更新技能时自动创建/更新模板，废弃/删除时自动清理
- 用户可手动编辑模板文件进行微调

## 反思触发策略

| 触发条件 | 说明 |
|---------|------|
| 定期反思 | 每 N 轮对话自动触发（默认 N=5） |
| 负反馈触发 | 用户 Ctrl+B 差评时异步触发 |
| 新领域检测 | 当前轮无任何技能匹配时触发 |
| 手动触发 | 用户输入 `/reflect` 命令 |

## 安全约束

- **技能上限**：活跃技能数量限制（默认 50），超出时按置信度淘汰
- **幻觉防护**：技能 instruction 只存策略指导，不存具体事实
- **版本控制**：每次更新递增 version，可回溯
- **人工审核（可选）**：CANDIDATE → ACTIVE 可配置为需要用户确认

## 目录结构

```
alex/skill/
├── __init__.py
├── models.py           # Skill 数据类
├── service.py          # SkillService — 构造函数注入全部依赖
├── repository.py       # SkillStore — JSON 持久化 + 模板管理
├── matcher.py          # SkillRetriever — 标签 + 关键词检索
├── reflector.py        # Reflector — LLM JSON-mode 反思引擎（支持 episodes）
├── evolution.py        # EvolutionEngine — 进化策略 & 生命周期 + 上限裁剪
└── ports.py            # SkillService Protocol（历史遗留）
```
