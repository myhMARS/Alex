# Alex

> A terminal-first AI agent built on Bun, TypeScript, and an event-driven module kernel.

[![Bun](https://img.shields.io/badge/Bun-1.4-black?logo=bun)](https://bun.sh)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.7-blue?logo=typescript)](https://www.typescriptlang.org/)
[![Tests](https://img.shields.io/badge/tests-305%20passing-brightgreen)](#development)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Alex runs an LLM agent loop in your terminal, with streaming replies, tool calling,
durable sessions, agent teams, skills, cron jobs, MCP servers, and an OpenTUI
interface. Every module communicates through a typed event bus; the TUI itself is
just another module.

## Features

- **Streaming agent loop** — content, thinking, tool calls, usage and errors
- **Terminal UI** — Markdown rendering, code highlighting, collapsible tool trees,
  approvals, file sidebar and session pages
- **Durable sessions** — append-only JSONL event log, resume, fork, compact, repair
- **Agent teams** — spawn workers, task queues, mailbox, lineage, per-agent permissions
- **Skills** — retrieval, explicit loading, reflection, deduplication and merge
- **MCP** — stdio and streamable HTTP / SSE servers; unavailable servers are excluded from the prompt and status changes are injected into the next model request
- **Cron** — persistent scheduled turns with retry/backoff
- **Model agnostic** — any OpenAI-compatible endpoint, with DeepSeek DSML support
- **Safe tools** — allow/deny/ask permission policy, approval previews, audit log

## Requirements

- [Bun](https://bun.sh) 1.4 or newer
- An OpenAI-compatible chat API key for model mode
- A terminal supported by OpenTUI

Without an API key, Alex starts in offline echo mode, which is useful for testing
the TUI and event bus.

## Quick Start

```bash
git clone https://github.com/myhMARS/Alex.git
cd Alex
bun install
bun run src/entry.ts --tui
```

Or install the CLI globally:

```bash
bun link
alex-ts --tui
```

## CLI

```text
alex-ts --tui        interactive terminal UI
alex-ts --demo       headless scripted demo
alex-ts --help       show help
alex-ts --version    show version
alex-ts --session ID open a specific session
alex-ts --no-mcp     disable MCP servers
```

## TUI

Type `/` to open the command palette, or use:

| Command | Purpose |
|---|---|
| `/new` | start a new session |
| `/sessions` | list and resume sessions |
| `/settings` | view configuration |
| `/set key value` | update a setting |
| `/context` | context-window usage |
| `/compact` | compact older history |
| `/agents` | agent team / tasks for this session |
| `/agents all` | all agents and tasks |
| `/tasks` | task list for this session |
| `/mailbox` | agent inbox |
| `/lineage` | agent and session tree |
| `/skills` | installed skills |
| `/reflect` | force a skill reflection pass |
| `/merge` | merge duplicate skills |
| `/cron` | scheduled jobs |
| `/mcp` | MCP server status |
| `/prompt` | assembled system prompt |
| `/help` | full command list |
| `/quit` | exit |

Common shortcuts:

| Key | Action |
|---|---|
| `Ctrl+S` | show/hide sidebar |
| `Ctrl+T` | expand thinking |
| `Ctrl+K` | expand skills |
| `Ctrl+O` | expand tool output |
| `Ctrl+C` | copy selection, or quit when nothing is selected |
| `Ctrl+G` / `Ctrl+B` | positive / negative feedback |
| `F1` / `F2` / `F3` | sidebar tabs |

Completed turns collapse their tool calls into a single `⚙ Tools (N)` line.
Click it to expand the tool-call tree.

## Configuration

Copy `.env_example` to `.env`:

```ini
ALEX_API_KEY=sk-...
ALEX_MODEL=deepseek-chat
ALEX_BASE_URL=https://api.deepseek.com
```

Frequently used variables:

| Variable | Default | Description |
|---|---|---|
| `ALEX_API_KEY` | empty | provider API key |
| `ALEX_MODEL` | `deepseek-chat` | model name |
| `ALEX_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint |
| `ALEX_CONTEXT_TOKENS` | `128K` | context window |
| `ALEX_MAX_TOKENS` | `4096` | max output tokens |
| `ALEX_THINKING` | `on` | thinking/reasoning switch |
| `ALEX_EFFORT` | `high` | `low`, `high`, `max` |
| `ALEX_MAX_ITERATIONS` | `10` | max tool-call iterations per turn |
| `ALEX_AUTO_REFLECT` | `on` | automatic skill reflection |
| `ALEX_REFLECTION_MIN_TOOLS` | `3` | tool calls required for reflection |

Settings are persisted to `~/.alex/settings.json` and can be changed at runtime:

```text
/set model deepseek-chat
/set max_context 1M
/set max_iterations 20
/set auto_reflect off
```

## Agent Teams

Workers are spawned through the normal agent loop and receive tasks through the
same event bus:

```text
main session
  ├─ worker-a
  │   └─ worker-b
  └─ worker-c
```

Each worker has an isolated tool policy, its own session, task queue and mailbox.
The TUI sidebar only shows agents belonging to the current session tree; disposed
workers disappear immediately. Use `/agents all` for the global view.

## Architecture

```text
                 ┌──────────────┐
                 │  TuiModule   │
                 └──────┬───────┘
                        │ events / commands
                 ┌──────▼───────┐
                 │  EventBus    │  typed Event / Command / Request
                 └──────┬───────┘
     ┌──────────┬───────┼────────┬──────────┬─────────┐
     ▼          ▼       ▼        ▼          ▼         ▼
  Agent      Session  Tools    Skills     Cron      MCP
  Module     Store    Module   Module     Module    Module
```

- `src/kernel/` — bus, module host, contracts and DTOs
- `src/modules/agent/` — streaming ReAct loop, compaction, tools
- `src/session/` — event projection, v3 JSONL store, token metering
- `src/tools/` — tool registry, permissions, audit, built-in tools
- `src/agents/` — agent registry, tasks, mailbox, lineage
- `src/tui/` — OpenTUI renderer and TUI module
- `src/llm/` — OpenAI-compatible streaming client and DSML parser

## Data

| Path | Content |
|---|---|
| `~/.alex/settings.json` | model and UI settings |
| `~/.alex/sessions/*.jsonl` | append-only session events |
| `~/.alex/skills/skills.json` | learned skills |
| `~/.alex/team/tasks.jsonl` | agent tasks |
| `~/.alex/team/agents.jsonl` | agent records |
| `~/.alex/team/mailbox.jsonl` | agent messages |
| `~/.alex/audit/permissions.jsonl` | tool approval audit |

## Development

```bash
bun install
bun run typecheck
bun test
bun run build        # dist/alex-ts.js
```

The test suite covers the kernel, session projection, agent loop, tool
permissions, MCP transport, skills/reflection, cron, TUI state and renderer.

## License

MIT — see [LICENSE](LICENSE).