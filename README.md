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
  <a href="#"><img src="https://img.shields.io/badge/tests-325%20passed-success" alt="Tests" /></a>
</p>

---

Alex is an agent that lives in your terminal — reads files, writes patches, runs shells, searches the web, schedules recurring jobs, and **gets better over time** as it distills strategies from past conversations.

### Why Alex?

- **Reaches into your project** — read files, search by content (`grep`) or name (`glob`), write atomic patches, run `bash` or `pwsh`
- **Web-connected** — DuckDuckGo search and clean page extraction
- **Cron scheduler** — Schedule prompt-driven background jobs with 5-field cron and optional durable persistence; durable jobs are restored after restart, rebound to the current session, and still run only while Alex is open and idle
- **Permission-gated** — Side-effect tools require explicit approval; you see a unified diff for every write before it lands
- **Auditable** — Every approval / denial appended to `~/.alex/audit/permissions.jsonl`
- **MCP-ready** — Auto-discovers Model Context Protocol servers from `~/.alex/mcp.json`, including stdio and HTTP transports
- **Plugin-friendly** — Drop a `*.py` file in `~/.alex/plugins/` to add your own tools
- **Sees its reasoning** — DeepSeek thinking mode reveals *how* the agent reached an answer
- **Gets better with use** — Distills reusable skills from conversations; thumbs up/down steer evolution
- **Markdown rendering** — Final responses render code blocks, lists, headings, inline code with proper terminal styling

## Installation

### Prerequisites

- **Python 3.12+**
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`
- Optional: `rg` (ripgrep) for fast `grep`; `bash` and/or `pwsh` for shell tools; `git` for `git_inspect`

### Setup

```bash
git clone https://github.com/<user>/alex.git
cd alex

# Recommended: uv handles the venv and lockfile for you
uv sync

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
| `/cron [query]` | Show current cron jobs |
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

For `write` and `edit`, the modal includes a unified diff against the current file so you can review every line that's about to change. For `bash` / `pwsh`, you see the full command, working directory, and timeout.

Every decision is appended to `~/.alex/audit/permissions.jsonl`.

### Tools Available to the Agent

| Tool | Permission | Purpose |
|------|------------|---------|
| `time` | — | Current date/time, timezone-aware |
| `web_search` | `network` | DuckDuckGo search |
| `web_fetch` | `network` | Clean text extraction from a URL |
| `cron` | — | Schedule prompt-driven background jobs with 5-field cron; `prompt` must be only the real task content, without reminder wrappers, elapsed-time wording, or decorative text |
| `read` | `read` | Read a text file with binary detection and size cap |
| `write` | `write` | Atomic full-file write — diff shown before approval |
| `edit` | `write` | Precise string replacement; requires prior `read` and detects external edits |
| `glob` | `read` | Find files by name pattern, sorted by `mtime` |
| `grep` | `read` | Regex content search (uses `rg` when available, pure-Python fallback) |
| `git_inspect` | `read` | Read-only `git status` / `diff` / `log` |
| `bash` | `shell` | Run a `bash -lc` command with hard deny list (`rm`, `dd`, `sudo`, …) |
| `pwsh` | `shell` | Run a PowerShell command with hard deny list (`Remove-Item`, `Format-Volume`, `iex`, …) |
| `load_skill` | — | Built-in: load full execution methodology for a skill |
| `cron_jobs` | — | Built-in: query current cron jobs, including durable jobs |
| MCP tools | `network` | Anything exposed by an MCP server in `~/.alex/mcp.json` |
| User plugins | — | Anything dropped into `~/.alex/plugins/*.py` |

### Cron Notes

- `durable=false` keeps a job only for the current app lifetime
- `durable=true` persists the job definition to `~/.alex/cron/`, restores it on restart, and rebinds it to the current session
- Restored jobs do not run while Alex is closed; they resume only when the TUI is open and idle
- The right-hand `后台任务` panel shows active cron jobs and refreshes the `next:` countdown every second
- The `prompt` should describe only the task to perform, not reminder phrasing like elapsed-time/status text or UI-style wrapper text

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
    "local-server": {
      "command": "your-mcp-command",
      "args": ["--your-arg", "value"]
    },
    "http-server": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer token-value",
        "X-Custom-Header": "custom-value"
      }
    },
    "sse-server": {
      "transport": "sse",
      "url": "http://localhost:8123/sse",
      "headers": {"Authorization": "Bearer token-value"},
      "timeout": 15,
      "sse_read_timeout": 60
    }
  }
}
```

Config rules:

- `command` means stdio transport
- `url` means HTTP transport and defaults to `streamable-http`
- `transport` currently accepts `stdio`, `streamable-http`, or `sse`
- `headers` are forwarded to HTTP MCP servers
- `timeout` and `sse_read_timeout` are optional HTTP transport settings
- `disabled: true` keeps the entry in config but skips connecting

Tools surface as `mcp__<server>__<tool>` and inherit the `network` permission. Servers connect on TUI start; failures appear as toasts and do not block the rest of the agent.

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

Alex is a modular monolith — a thin TUI layer wired to 5 application services
through a shared composition root, with an event bus for cross-cutting
projection and persistence.

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
│   ├─ CronService       (scheduler lifecycle)│
│   ├─ FeedbackAppService (rating/reflect)    │
│   └─ SkillAdminAppService (CRUD/merge)      │
│                                             │
│   Wiring: composition.create_agent()        │
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
│   ├── composition.py          # Shared default-dependency constructors
│   ├── chat_service.py         # chat_stream, tool exec, graph
│   ├── session_service.py      # persistence boundary
│   ├── cron_service.py         # scheduler lifecycle (57-line wrapper)
│   ├── feedback_service.py     # rating, episodes, reflection
│   ├── skill_admin_service.py  # skill CRUD, merge
│   ├── turn_processor.py       # unified user/cron turn FIFO processor
│   ├── prompt.py               # PromptAssembler
│   └── ports.py                # AgentFacade Protocol
├── bus/                        # Event bus
│   ├── events.py               # typed event hierarchy
│   └── in_memory.py            # AsyncEventBus
├── memory/                     # Conversation memory
│   ├── base.py                 # MemoryBase ABC
│   ├── buffer.py               # BufferMemory (sliding window)
│   └── ports.py                # MemoryService Protocol
├── skill/                      # Adaptive skill system
│   ├── service.py              # business logic
│   ├── repository.py           # SkillStore (JSON, atomic writes)
│   ├── matcher.py              # tag+keyword retrieval
│   ├── reflector.py            # LLM reflection
│   ├── evolution.py            # lifecycle transitions
│   └── ports.py                # SkillServicePort Protocol
├── store/                      # Session persistence
│   ├── session.py              # file I/O
│   ├── session_serializer.py   # BaseMessage <-> dict
│   ├── session_adapter.py      # SessionPersistence (event-driven)
│   └── ports.py                # SessionRepository Protocol
├── scheduler/
│   └── manager.py              # CronManager — APScheduler + job lifecycle
├── tools/
│   ├── registry.py / executor.py / ports.py
│   ├── permissions.py          # PermissionPolicy + AuditLogger + summarisers
│   ├── plugin_loader.py        # ~/.alex/plugins/*.py discovery
│   ├── mcp_client.py           # MCP multi-transport client
│   ├── fs.py                   # read / write / edit + FileReadTracker
│   ├── search.py               # grep / glob
│   ├── shell.py                # bash / pwsh
│   ├── git.py                  # git_inspect
│   ├── time.py / web_search.py / web_fetch.py / cron.py
│   └── _path.py / _binary.py   # shared OS-level helpers
├── tui/
│   ├── app.py                  # AlexApp — Textual root
│   ├── controller.py           # commands, session, toggles
│   ├── chat_projector.py       # bus → widget projection
│   ├── notification_controller.py # toast, feedback, permission modal
│   ├── confirm_screen.py       # PermissionConfirmScreen
│   ├── view_state.py / view_models.py / cron_history.py
│   ├── presenter.py            # AlexBubble / UserBubble / ToolBubble
│   ├── stream_renderer.py
│   ├── markdown.py             # Rich Markdown rendering layer
│   └── alex.tcss               # externalized TUI stylesheet
├── llm/                        # Multi-provider LLM layer
│   ├── factory.py              # LLMFactory — provider dispatch
│   ├── base.py                 # LLMConfig
│   ├── openai.py / deepseek.py / anthropic.py
│   └── json_client.py          # JSON-mode client with digest-based caching
├── prompts/                    # Jinja2 templates
├── config.py
└── prompts/                    # Jinja2 prompt templates (system, skills, reflection)
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

325 tests covering the agent core, TUI rendering, tool gating, permission policy, audit log, plugin loader, MCP client, Markdown layer, contract/state/event semantics across port boundaries, and CronStore/CronExecutor unit tests. Tests dependent on optional binaries (`bash`, `pwsh`, `git`, `rg`) skip gracefully when those aren't installed.

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
