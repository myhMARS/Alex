# Python → TypeScript parity

The repository is now a **single TypeScript implementation**; the legacy Python
tree was removed on 2026-11-19 after the user-approved cut-over. This document
records the final parity state and the intentionally remaining differences.

## Automated parity checks

`bun test tests/parity.test.ts`:

1. Every kernel contract in the frozen Python snapshot
   (`tests/fixtures/python-contracts.json`) has an exported TS counterpart
   (`src/kernel/contracts/*.ts`) — **41 / 41**.
2. Every built-in tool name in the frozen snapshot exists in the TS tool catalog
   (builtins + skills + cron runtime tools); the only TS extras are
   `load_skill` / `list_skills`.
3. TS loaders accept the Python-shaped JSON fixtures for sessions, skills and
   cron jobs (snake_case preserved on disk).

## Status matrix

Legend: ✅ parity · 🟡 core ported, gaps documented · ⏳ not ported / intentionally deferred

| Area | Python | TS | Status |
|---|---|---|---|
| Kernel contracts / DTOs | `kernel/contracts`, `dto` | `kernel/contracts`, `dto` | ✅ 41/41 contracts |
| Event bus | `bus/in_memory.py` | `kernel/bus.ts` | ✅ buffering, per-session serialization, request timeout/errors |
| ModuleHost | `kernel/host.py` | `kernel/runtime.ts` | ✅ + explicit cycle detection |
| Memory | `memory/` | `modules/memory/` | ✅ sliding window |
| Session store | `store/` | `modules/store/` | ✅ same `~/.alex/sessions/*.json` format |
| Tools registry / permissions / approval / audit | `tools/` | `tools/` | ✅ same `~/.alex/audit/permissions.jsonl` JSONL shape |
| Built-in tools | 15 names | 17 names | ✅ all Python tools incl. `bash_wsl`; + `load_skill`, `list_skills`；参数已对齐：`edit.file_path`、`read.max_bytes/encoding`、`write.create_dirs/encoding`、`grep` 高级选项、`web_fetch.max_length`、`shell cwd/timeout_seconds`、`web_search` 上限 15 |
| Tool plugins | `~/.alex/plugins/*.py` | `.ts/.js` in `pluginDirs` | 🟡 Python plugins not loadable (documented) |
| MCP | `mcp/` (SDK) | `mcp/` (JSON-RPC) | ✅ stdio + streamable-http + **legacy HTTP+SSE transport** + periodic 30s health loop |
| LLM client | `llm/client.py` | `llm/client.ts` | ✅ streaming content/thinking/tool-call deltas, usage, errors |
| Agent loop | `agent/turn_processor.py` | `modules/agent/module.ts` | ✅ tool loop, max-iterations fallback, cron turns, skill prompt injection |
| Skills | `skill/` | `skill/` | ✅ store/retriever/reflection/tools + feedback reflection + LLM merge + lifecycle evolution + **Python-compatible `prompts/<id>.j2` templates**（add/update 写入、deprecate/remove 删除，`templateFor` 可读；Python 侧 `to_prompt_text` 同样未被调用，episodes 两侧始终为 `[]`） |
| Cron | `scheduler/` | `cron/` | 🟡 5-field cron (local tz), durable JSON, one-shot cleanup, restart restore; no `CronDebugEvent`, no per-session locks, no session rebinding after first turn |
| Prompts | Jinja2 templates (`prompts/*.j2`) | `src/prompts/` builders | ✅ 系统/反思/技能段/合并/技能卡构建器；per-skill `.j2` 模板文件与 Python 内容一致（不实现 Jinja 变量语法，模板为静态文本） |
| TUI | Textual + OpenTUI/Ink over WS | OpenTUI in-process `src/tui/` | ✅ chat/streaming/tools/approvals、总线命令页、会话恢复、反馈快捷键、MCP 展开、富 Markdown、代码高亮、状态侧栏、Ctrl+T/K/O、toast |
| Approval previews | unified diff for write/edit | unified diff for write/edit | ✅ `src/tools/diff.ts` + 审批弹窗展示 |
| CLI | `alex --tui ink` | `alex-ts --tui/--demo/--help/--version/--session/--no-mcp` | ✅ flags + `bun link` install; bundle via `bun run build` |
| Packaging | uv tool + bundled node runtime zip | `bun link` / `dist/alex-ts.js` | 🟡 single-file binary blocked by OpenTUI native FFI (documented) |
| Tests | 388 pytest (removed with legacy tree) | 151 bun tests | ✅ frozen Python contract/tool/JSON snapshots retained as fixtures |

## P6 finish line (ordered)

1. **TUI command pages** — done: `/help`, `/skills`, `/cron`, `/mcp`, `/sessions`
   (bus requests), `/resume`-equivalent session loading via LoadSession +
   ReplaceMemory + view rebuild, session digit selection, Ctrl+G/B feedback.
   Remaining: MCP server toggle from the MCP page.
2. ~~Approval diff preview~~ — done (unified diff for `write`/`edit`).
3. ~~MCP health loop~~ — done (30s `tools/list` refresh, status events, stale
   tools removed, failures marked ERROR, legacy SSE transport).
4. ~~Skill parity~~ — done (merge, evolution, per-skill templates; episodes are
   not accumulated in Python either — it always passes `[]`).
5. **Cross-language golden tests** — replaced by frozen Python fixtures in
   `tests/fixtures/python-contracts.json` plus Python-shaped JSON fixtures.
6. ~~Cut-over~~ — done on 2026-11-19: legacy Python tree removed, TS promoted to
   repository root (`src/`, `tests/`, `package.json`).
7. Remaining polish: OpenTUI native-FFI packaging (single-file binary), optional
   Python `.py` tool-plugin compatibility (intentionally not supported).

## Known differences that are intentional

- TS messages use camelCase on the bus; **on-disk JSON stays snake_case** for
  session/skill/cron compatibility with Python.
- `SkillsReflected.new` is `created` in TS (`new` is a reserved word).
- TS dependencies: `agent → memory, tools, skill` and `tui → agent`; Python's
  entry loads the TUI outside the module host. TS makes the TUI a real module.
- TS tools return error text for expected failures; the bus still surfaces
  provider errors via `ToolResult.error`. Python behaves the same way.