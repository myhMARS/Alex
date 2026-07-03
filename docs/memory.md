# 上下文/记忆管理 (`alex/memory/`)

## 设计思路

定义 `MemoryBase` 抽象接口，`MemoryModule` 将其包装为 bus request handler。Agent 通过 `bus.request(GetContext(...))` 获取上下文，不直接依赖 Memory 实现。

## MemoryModule

`module.py` 是记忆模块的 bus 入口，启动时注册 4 个 request handler：

| Handler | Request 类型 | 返回 |
|---------|-------------|------|
| `_handle_get_context` | `GetContext` | `list[dict]` |
| `_handle_append` | `AppendMessages` | `None` |
| `_handle_replace` | `ReplaceMemory` | `None` |
| `_handle_clear` | `ClearMemory` | `None` |

模块无依赖（`dependencies: list[str] = []`），最先启动。

## 接口定义 (MemoryBase)

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
- `get_context_sync()` 提供同步读取
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
├── module.py          # MemoryModule — bus 入口
├── base.py            # MemoryBase 抽象接口
├── buffer.py          # BufferMemory 滑动窗口（默认实现）
└── factory.py         # MemoryModule 工厂函数
```
