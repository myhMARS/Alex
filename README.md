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
</p>

---

Alex is an agent that lives in your terminal — search the web, fetch pages, schedule recurring jobs, and watch it **improve over time** as it distills strategies from past conversations.

### Why Alex?

- **Web-connected** — Search with DuckDuckGo and fetch page content, so answers stay current
- **Cron scheduler** — Schedule background jobs with interval or crontab, subscribe to results inline
- **Sees its reasoning** — DeepSeek thinking mode shows you *how* it arrived at an answer
- **Gets better with use** — Distills reusable skills from conversations; you rate responses to steer evolution
- **Terminal-first** — Full TUI with scrollback, collapsible details, session persistence — no browser needed

## Installation

### Prerequisites

- **Python 3.12+**
- [uv](https://docs.astral.sh/uv/) (recommended) or `pip`

### Setup

```bash
git clone https://github.com/<user>/alex.git
cd alex

# Install dependencies
uv sync
# or: pip install -e .
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

## Usage

```bash
# Interactive TUI (default)
python main.py

# One-shot query
python main.py "What's the weather in Tokyo today?"

# Streaming output
python main.py --stream "Explain the reactor pattern in async Python"
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
| `/resume` | Restore a saved session |
| `/clear` | Clear current session |
| `/quit` | Exit |

### Tools Available to the Agent

| Tool | What it does |
|------|-------------|
| `web_search` | Search the web via DuckDuckGo |
| `web_fetch` | Extract clean text from a URL |
| `time` | Get current date/time (timezone-aware) |
| `cron` | Schedule recurring background jobs (interval or crontab) |

### Cron Tool Quick Example

```
You: Search "AI news" every 10 minutes and tell me the top headline

Alex: [schedules a cron job via the cron tool]

# 10 minutes later, a new bubble appears in chat with the latest headline
```

## How Skills Work

Alex learns reusable strategies from past problem-solving episodes:

1. **Record** — Each conversation turn captures which tools and skills were used
2. **Reflect** — Every 5 turns (or on demand), an LLM analyzes recent experience and extracts new methodologies
3. **Retrieve** — On each query, relevant skills are matched by tag + keyword scoring
4. **Apply** — The agent loads a skill's full execution guide on demand via the `load_skill` tool
5. **Evolve** — Skills that perform well (≥70% success rate) graduate from CANDIDATE to ACTIVE; poor performers are deprecated

Rate responses with <kbd>Ctrl+G</kbd> / <kbd>Ctrl+B</kbd> to steer which skills survive.

## Architecture

```
main.py ──────────────────────────────────────────────────────
  │                    │
  ▼                    ▼
TUI (Textual)     CLI (Rich)
  │                    │
  └────────┬───────────┘
           ▼
   ┌──────────────┐    ┌──────────────┐
   │    Agent     │───▶│  LLM Factory │──▶ DeepSeek / OpenAI / Anthropic
   │  (LangGraph) │    └──────────────┘
   └──┬───┬───┬───┘
      │   │   │
      ▼   ▼   ▼
   Tools  Memory  Skills
   ┌──┐  ┌─────┐ ┌────────────┐
   │WS│  │Buffer│ │Retrieve    │
   │WF│  │Memory│ │Reflect     │
   │TI│  └─────┘ │Evolve      │
   │CR│          │SkillStore  │
   └──┘          └────────────┘
```

## Project Structure

```
alex/
├── agent.py           Agent core — chat, streaming, notifications, reflection
├── config.py          .env → LLMConfig
├── cron.py            APScheduler-backed background job manager
├── callbacks.py       LangChain callback → Rich display event bridge
├── display.py         Rich terminal renderer, ThinkingDisplay, event queue
├── tui.py             Textual TUI application (~1200 lines)
├── llm/               Multi-provider LLM factory (DeepSeek, OpenAI, Anthropic)
├── memory/            Pluggable memory (BufferMemory default, RAG-ready interface)
├── skills/            Adaptive skill lifecycle (retrieve, reflect, evolve, merge)
├── streaming/         StreamEvent types + StreamHandler
├── tools/             web_search, web_fetch, time, cron
└── prompts/           Jinja2 templates (system, reflection, skills, merge)
```

## Documentation

Full module docs in [`docs/`](docs/):

| Document | Covers |
|----------|--------|
| [design.md](docs/design.md) | Architecture overview and future roadmap |
| [agent.md](docs/agent.md) | Agent orchestration, streaming, notifications, cron integration |
| [display.md](docs/display.md) | Textual TUI, shortcuts, feedback, session management |
| [llm.md](docs/llm.md) | Factory pattern, adapter registration, JSON mode client |
| [memory.md](docs/memory.md) | Abstract memory interface, BufferMemory, extension points |
| [streaming.md](docs/streaming.md) | Stream event types and handler |
| [skills.md](docs/skills.md) | Skill lifecycle, reflection, retrieval, merging, templates |
| [config.md](docs/config.md) | Environment variable configuration |

## Tech Stack

- **[LangChain](https://www.langchain.com/) + [LangGraph](https://langchain-ai.github.io/langgraph/)** — Agent framework and graph-based execution with streaming
- **[Textual](https://textual.textualize.io/)** — Rich TUI framework (alternate screen, CSS layout, widgets)
- **[Rich](https://rich.readthedocs.io/)** — Beautiful terminal rendering (Markdown, panels, tables)
- **[APScheduler](https://apscheduler.readthedocs.io/)** — Background job scheduling (interval + crontab)
- **[httpx](https://www.python-httpx.org/) + [BeautifulSoup4](https://www.crummy.com/software/BeautifulSoup/)** — Async web fetching and content extraction
- **[DuckDuckGo](https://github.com/deedy5/duckduckgo_search)** — Anonymous web search
- **[Jinja2](https://jinja.palletsprojects.com/)** — Prompt template engine with file-system loader
- **[Pydantic](https://docs.pydantic.dev/)** — Data validation and settings

## Contributing

Contributions are welcome! Areas where help would be especially valuable:

- New tools (file system, database, API connectors)
- RAG memory backends (vector DB integration)
- Web API (FastAPI + SSE/WebSocket streaming endpoint)
- More LLM provider adapters
- Windows terminal compatibility improvements

Please open an issue to discuss before submitting large PRs.

## License

[MIT](LICENSE)
