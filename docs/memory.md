# 上下文/记忆管理层 (`alex/memory/`)

## 设计思路

定义 `MemoryBase` 抽象接口，Agent 只依赖接口。当前用 `BufferMemory` 实现，后续可替换为 Mem0、MemGPT、向量数据库等。

## 接口定义

| 方法 | 说明 |
|------|------|
| `add_message(msg)` | 添加单条消息 |
| `add_messages(msgs)` | 批量添加 |
| `get_context(query?)` | 获取上下文（可选传入 query 做相关性检索） |
| `clear()` | 清空记忆 |
| `size` | 当前消息数量 |
| `summarize()` | 可选 — 生成记忆摘要 |
| `search(query, top_k)` | 可选 — 语义搜索（RAG 场景） |

## 默认实现：BufferMemory

- 保留最近 N 条消息（滑动窗口，默认 100 条）
- `get_context` 直接返回全部缓冲消息
- `get_context_sync()` 提供同步读取（Agent 内部使用）
- 无持久化，进程结束即丢失
- **线程安全**：内部 `_write_lock`（`asyncio.Lock`）序列化写操作，`add_messages()` 一次获取锁写入全部消息，保证批量写入原子性

## 扩展预留

- `get_context(query)` 的 query 参数为 RAG 检索预留
- `search()` 方法为向量记忆预留
- `summarize()` 为摘要压缩预留

## 目录结构

```
alex/memory/
├── __init__.py
├── base.py           # MemoryBase 抽象接口（含 get_context_sync）
└── buffer.py         # 滑动窗口缓冲记忆（默认实现）
```
