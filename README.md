<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)">
    <img alt="Alex" src="https://img.shields.io/badge/Alex-AI%20Agent-10b981?style=for-the-badge" />
  </picture>
</p>

<p align="center">
  <strong>A terminal-native AI agent that learns from every conversation.</strong>
</p>

<p align="center">
  <a href="#license"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+" /></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey" alt="Platform" /></a>
  <a href="#"><img src="https://img.shields.io/badge/tests-258%20passing-success" alt="Tests" /></a>
</p>

---

Alex is an agent that lives in your terminal — reads files, writes patches, runs shells, searches the web, schedules recurring jobs, and **gets better over time** as it distills strategies from past conversations.

### Why Alex?

- **Reaches into your project** — read files, search by content (`grep`) or name (`glob`), write atomic patches, run `bash` or `pwsh`
- **Web-connected** — DuckDuckGo search and clean page extraction
- **Cron scheduler** — Schedule background jobs (interval or crontab) and stream results back into chat
- **Permission-gated** — Side-effect tools require explicit approval; you see a unified diff for every write before it lands
- **Auditable** — Every approval / denial appended to `~/.alex/audit/permissions.jsonl`
- **MCP-ready** — Auto-discovers Model Context Protocol servers from `~/.alex/mcp.json`
- **Plugin-friendly** — Drop a `*.py` file in `~/.alex/plugins/` to add your own tools
- **Sees its reasoning** — DeepSeek thinking mode reveals *how* the agent reached an answer
- **Gets better with use** — Distills reusable skills from conversations; thumbs up/down steer evolution
- **Markdown rendering** — Final responses render code blocks, lists, headings, inline code with proper terminal styling

## Installation

### Prerequisites

- **Python 3.12+**
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- Optional: `rg` (ripgrep) for fast `grep`; `bash` and/or `pwsh` for shell tools; `git` for `git_inspect`
- Optional: `mcp` Python SDK if you want to use MCP servers

### Setup

```bash
git clone https://github.com/<user>/alex.git
cd alex

# Recommended: uv handles the venv and lockfile for you
uv sync

# Optional: install MCP client support
uv pip install -e ".[mcp]"
```

### Configure

```bash
cp .env_example .env
```

Edit `.env` — at minimum set your API key:

| Variable | Description | Default |
|----------|-------------|---------|
| `ALEX_PROVIDER` | LLM provider (`deepseek`, `openai`, `anthropic`) | `deepseek` |
| `ALEX_API_KEY` | API key *(required)* | — |
| `ALEX_BASE_URL` | API base URL | `https://api.deepseek.com` |
| `ALEX_MODEL` | Model name | `deepseek-chat` |
| `ALEX_MAX_TOKENS` | Max tokens per response | `8192` |
| `ALEX_TEMPERATURE` | Sampling temperature | `0.0` |
| `ALEX_TOOL_PERMISSIONS` | Comma list of permissions to allow without prompting | `read,network` |
| `ALEX_TOOL_DENY` | Comma list of permissions to always deny | — |
| `ALEX_TUI_MARKDOWN` | Set to `0` to disable Markdown rendering | `1` |
| `ALEX_CRON_DEBUG` | Set to `1` for verbose cron logs | — |

## Usage

```bash
uv run python main.py
```

### TUI Shortcuts

| Key | Action |
|-----|--------|
| <kbd>Ctrl+T</kbd> | Toggle thinking / reasoning visibility |
| <kbd>Ctrl+K</kbd> | Toggle skill blocks |
| <kbd>Ctrl+G</kbd> | Rate last response helpful |
| <kbd>Ctrl+B</kbd> | Rate last response unhelpful (triggers reflection) |
| <kbd>Ctrl+C</kbd> | Quit |

### TUI Commands

| Command | Action |
|---------|--------|
| `/help` | Show all commands and shortcuts |
| `/skills` | Browse learned skills with usage stats |
| `/skills del <id>` | Delete a skill by name or prefix |
| `/skills dep <id>` | Deprecate a skill |
| `/merge-skills` | LLM-powered skill deduplication |
| `/reflect` | Force a reflection cycle |
| `/cron [query]` | Show this session's cron execution history |
| `/resume` | Restore a saved session |
| `/clear` | Clear current session |
| `/quit` | Exit |
| `:q` | Close any overlay panel |
| `/x` | Dismiss the current toast |

### Permission Confirm Modal

When the agent calls a tool that needs an unusual permission (e.g. writing a file or running a shell command), Alex pauses with a modal showing the proposed change before running it:

| Key | Decision |
|-----|----------|
| <kbd>Y</kbd> / <kbd>A</kbd> | Allow once |
| <kbd>S</kbd> | Allow for the rest of this session |
| <kbd>N</kbd> / <kbd>Esc</kbd> | Deny — the tool returns a `blocked` error and the model can adapt |

For `fs_write` and `edit`, the modal includes a unified diff against the current file so you can review every line that's about to change. For `bash` / `pwsh`, you see the full command, working directory, and timeout.

Every decision is appended to `~/.alex/audit/permissions.jsonl`.

### Tools Available to the Agent

| Tool | Permission | Purpose |
|------|------------|---------|
| `time` | — | Current date/time, timezone-aware |
| `web_search` | `network` | DuckDuckGo search |
| `web_fetch` | `network` | Clean text extraction from a URL |
| `cron` | — | Schedule background jobs (interval or crontab) |
| `fs_read` | `read` | Read a text file with binary detection and size cap |
| `fs_write` | `write` | Atomic full-file write — diff shown before approval |
| `edit` | `write` | Precise string replacement; requires prior `fs_read` and detects external edits |
| `glob` | `read` | Find files by name pattern, sorted by `mtime` |
| `grep` | `read` | Regex content search (uses `rg` when available, pure-Python fallback) |
| `git_inspect` | `read` | Read-only `git status` / `diff` / `log` |
| `bash` | `shell` | Run a `bash -lc` command with hard deny list (`rm`, `dd`, `sudo`, …) |
| `pwsh` | `shell` | Run a PowerShell command with hard deny list (`Remove-Item`, `Format-Volume`, `iex`, …) |
| `load_skill` | — | Built-in: load full execution methodology for a skill |
| `cron_history` | — | Built-in: query this session's cron executions |
| MCP tools | `network` | Anything exposed by an MCP server in `~/.alex/mcp.json` |
| User plugins | — | Anything dropped into `~/.alex/plugins/*.py` |

### Cron Tool Quick Example

```
You: Search "AI news" every 10 minutes and tell me the top headline

Alex: [schedules a cron job via the cron tool]

# 10 minutes later, a new bubble appears in chat with the latest headline
```

### MCP Servers

Drop a config into `~/.alex/mcp.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "uvx",
      "args": ["mcp-server-filesystem", "/Users/me/Notes"]
    },
    "github": {
      "command": "uvx",
      "args": ["mcp-server-github"],
      "env": {"GITHUB_TOKEN": "ghp_xxx"}
    }
  }
}
```

Tools surface as `mcp__<server>__<tool>` and inherit the `network` permission. Servers connect lazily on TUI start; failures appear as toasts and don't block the rest of the agent.

### User Plugins

Drop any `.py` file into `~/.alex/plugins/`. Three entrypoint shapes are supported (matched in this order):

```python
# Module-level constant
ALEX_TOOLS = [my_tool, another_tool]

# Factory function
def tools() -> list[BaseTool]: ...

# Free-form registration
def register(agent) -> None:
    agent.register_tool(my_tool)
```

Files starting with `_` are skipped, broken plugins are isolated from each other.

## How Skills Work

Alex learns reusable strategies from past problem-solving episodes:

1. **Record** — Each turn captures which tools and skills were used
2. **Reflect** — Every 5 turns (or on demand), an LLM analyzes recent experience and extracts new methodologies
3. **Retrieve** — On each query, relevant skills are matched by tag + keyword scoring
4. **Apply** — The agent loads a skill's full execution guide on demand via the `load_skill` tool
5. **Evolve** — Skills that perform well (≥70% success rate) graduate from `CANDIDATE` to `ACTIVE`; poor performers are deprecated
6. **Merge** — `/merge-skills` runs an LLM pass that consolidates near-duplicate skills

Rate responses with <kbd>Ctrl+G</kbd> / <kbd>Ctrl+B</kbd> to steer which skills survive.

## Architecture

```
main.py
  │
  ▼
┌─────────────────────────────────────────────┐
│              TUI (Textual)                  │
│  ChatProjector  NotificationController      │
│  StreamRenderer  PermissionConfirmScreen    │
└──────────────────────┬──────────────────────┘
                       │  AgentFacade
                       ▼
┌─────────────────────────────────────────────┐
│   Agent (thin facade)                       │
│   ├─ ChatAppService    (chat, tools, graph) │
│   ├─ SessionService    (persistence)        │
│   ├─ CronService       (scheduler)          │
│   ├─ FeedbackAppService (rating/reflect)    │
│   └─ SkillAdminAppService (CRUD/merge)      │
└──┬──────────┬──────────┬──────────┬─────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
 Tools     Memory     Skills      LLM
 ─────     ──────     ──────     ─────
 fs/edit   Buffer     Retrieve   DeepSeek
 grep/glob (sliding)  Reflect    OpenAI
 bash/pwsh            Evolve     Anthropic
 git_*                Merge      ...
 web_*                Store
 cron
 MCP
 plugins
       │
       ▼
   Permissions  ←  AuditLogger (~/.alex/audit/permissions.jsonl)
       │
       ▼
   AsyncEventBus  ←→  Store (sessions)
```

## Project Structure

```
alex/
├── agent/                      # Application layer
│   ├── service.py              # Agent — thin facade
│   ├── factory.py              # create_agent() — wiring + plugin install
│   ├── chat_service.py         # chat_stream, tool exec, graph
│   ├── session_service.py      # persistence boundary
│   ├── cron_service.py         # scheduler lifecycle
│   ├── feedback_service.py     # rating, episodes, reflection
│   ├── skill_admin_service.py  # skill CRUD, merge
│   ├── orchestrator.py         # TurnOrchestrator
│   ├── cron_handler.py         # cron-triggered LLM replies
│   ├── prompt.py               # PromptAssembler
│   └── ports.py                # AgentFacade Protocol
├── bus/                        # Event bus
│   ├── events.py               # Event → Command/DomainEvent/UIEvent
│   └── in_memory.py            # AsyncEventBus
├── memory/                     # Conversation memory
│   ├── base.py
│   └── buffer.py               # BufferMemory (sliding window)
├── skill/                      # Adaptive skill system
│   ├── service.py              # business logic
│   ├── repository.py           # SkillStore (JSON)
│   ├── matcher.py              # tag+keyword retrieval
│   ├── reflector.py            # LLM reflection
│   └── evolution.py            # lifecycle transitions
├── store/                      # Session persistence
│   ├── session.py              # file I/O
│   ├── session_serializer.py   # BaseMessage <-> dict
│   └── session_adapter.py      # SessionPersistence (event-driven)
├── scheduler/
│   └── manager.py              # APScheduler wrapper
├── tools/
│   ├── registry.py / executor.py / ports.py
│   ├── permissions.py          # PermissionPolicy + AuditLogger + summarisers
│   ├── plugin_loader.py        # ~/.alex/plugins/*.py discovery
│   ├── mcp_client.py           # MCP stdio client
│   ├── fs.py                   # fs_read / fs_write / edit + FileReadTracker
│   ├── search.py               # grep / glob
│   ├── shell.py                # bash / pwsh
│   ├── git.py                  # git_inspect
│   ├── time.py / web_search.py / web_fetch.py / cron.py
├── tui/
│   ├── app.py                  # AlexApp — Textual root
│   ├── controller.py           # commands, session, toggles
│   ├── chat_projector.py       # bus → widget projection
│   ├── notification_controller.py # toast, feedback, permission modal
│   ├── confirm_screen.py       # PermissionConfirmScreen
│   ├── view_state.py / view_models.py / cron_history.py
│   ├── presenter.py            # AlexBubble / UserBubble / ToolBubble
│   ├── stream_renderer.py
│   └── markdown.py             # Rich Markdown rendering layer
├── llm/                        # provider factory + JSON-mode client
├── prompts/                    # Jinja2 templates
└── config.py
```

## Documentation

Full module docs in [`docs/`](docs/):

| Document | Covers |
|----------|--------|
| [design.md](docs/design.md) | Architecture overview, business goals, project tree |
| [agent.md](docs/agent.md) | Agent facade, application services, turn orchestration |
| [display.md](docs/display.md) | Textual TUI, shortcuts, feedback, Markdown rendering, confirm modal |
| [tools.md](docs/tools.md) | Tool registry, permission policy, audit log, MCP, plugins, every built-in tool |
| [llm.md](docs/llm.md) | Multi-provider factory, JSON-mode client |
| [memory.md](docs/memory.md) | Memory abstraction and BufferMemory |
| [skills.md](docs/skills.md) | Skill lifecycle, reflection, retrieval, merging |
| [streaming.md](docs/streaming.md) | LangGraph stream events |
| [events.md](docs/events.md) | Typed event hierarchy |
| [config.md](docs/config.md) | Environment variables and `LLMConfig` |
| [refactor-modular-architecture.md](docs/refactor-modular-architecture.md) | Refactor blueprint and phase log |
| [roadmap-future-evolution.md](docs/roadmap-future-evolution.md) | Future direction (skills, cron, observability, multi-frontend) |

## Tech Stack

- **[LangChain](https://www.langchain.com/) + [LangGraph](https://langchain-ai.github.io/langgraph/)** — Agent framework with streaming graph execution
- **[Textual](https://textual.textualize.io/)** — TUI framework (alternate screen, CSS layout, modal screens)
- **[Rich](https://rich.readthedocs.io/)** — Markdown / syntax highlighting / panel rendering
- **[APScheduler](https://apscheduler.readthedocs.io/)** — Cron and interval scheduling
- **[httpx](https://www.python-httpx.org/) + [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)** — Async web fetching and HTML extraction
- **[ripgrep](https://github.com/BurntSushi/ripgrep)** *(optional)* — Powering `grep` when present
- **[MCP](https://modelcontextprotocol.io/)** *(optional)* — External tool servers
- **[Pydantic](https://docs.pydantic.dev/)** — Tool schemas and validation
- **[Jinja2](https://jinja.palletsprojects.com/)** — Prompt templates

## Testing

```bash
uv run pytest -q
```

258 tests covering the agent core, TUI rendering, tool gating, permission policy, audit log, plugin loader, MCP client, and Markdown layer. Tests dependent on optional binaries (`bash`, `pwsh`, `git`, `rg`) skip gracefully when those aren't installed.

## Contributing

Welcome contributions:

- New tools (database, API connectors, language servers)
- Better permission UX (per-tool / per-pattern allow lists)
- RAG memory backends (vector DB)
- FastAPI / SSE / WebSocket frontends
- Additional LLM provider adapters

Please open an issue to discuss before submitting large PRs.

## License

[MIT](LICENSE)
