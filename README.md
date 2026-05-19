# Alex

An AI agent with web tools, streaming output, and adaptive skills — runs in your terminal.

## Features

- **Multi-provider LLM** — DeepSeek (thinking mode), OpenAI, Anthropic via factory pattern
- **Web tools** — `web_search` (DuckDuckGo), `web_fetch` (HTTP content extraction), `time` (timezone-aware)
- **Cron scheduler** — Background recurring jobs with interval or crontab expressions, subscribe to results
- **Streaming output** — Token-level streaming with thinking/reasoning content (DeepSeek reasoning_content)
- **Adaptive skills** — Agent learns from conversations, distills execution methodologies, evolves over time
- **TUI interface** — Textual-based terminal UI with scrollable history, collapsible thinking/skills, session persistence
- **User feedback** — Rate responses (Good/Bad) to drive skill evolution
- **CLI modes** — Single query and streaming CLI modes alongside the full TUI

## Quick Start

### Prerequisites

- Python >= 3.12
- `uv` package manager (recommended) or `pip`

### Installation

```bash
git clone <repo-url>
cd Alex

# Create virtual environment and install dependencies
uv sync
# or: pip install -e .
```

### Configuration

Copy the example env file and fill in your API key:

```bash
cp .env_example .env
```

Edit `.env`:

```env
ALEX_PROVIDER=deepseek
ALEX_API_KEY=sk-your-api-key-here
ALEX_BASE_URL=https://api.deepseek.com
ALEX_MODEL=deepseek-chat
```

Supported providers: `deepseek`, `openai`, `anthropic`.

### Run

```bash
# Interactive TUI mode (default)
python main.py

# Single query (non-TUI)
python main.py "What's the weather in Beijing?"

# Streaming CLI mode
python main.py --stream "Search for latest Python 3.13 release notes"
```

## Usage

### TUI Mode

| Shortcut | Action |
|----------|--------|
| `Ctrl+T` | Toggle thinking blocks |
| `Ctrl+K` | Toggle skill blocks |
| `Ctrl+G` | Rate last response as helpful |
| `Ctrl+B` | Rate last response as unhelpful (triggers reflection) |
| `Ctrl+C` | Quit |

| Command | Action |
|---------|--------|
| `/help` | Show all commands and shortcuts |
| `/skills` | List all learned skills |
| `/skills del <id>` | Delete a skill |
| `/skills dep <id>` | Deprecate a skill |
| `/merge-skills` | LLM-based skill deduplication |
| `/reflect` | Force skill reflection |
| `/resume` | Resume a saved session |
| `/clear` | Clear current session |
| `/quit` | Exit |

## Architecture

```
main.py (entry point)
  ├── TUI mode (Textual) ──► alex/tui.py (AlexApp)
  └── CLI mode (Rich)   ──► alex/display.py
              │
              ▼
      Agent (core orchestration)
  ┌───────────┼───────────┬──────────────┐
  ▼           ▼           ▼              ▼
Tools       Memory      Skills          LLM
(web_search (BufferMemory (SkillManager  (Factory:
 web_fetch   sliding      + Reflector    DeepSeek
 time        window)       + Retriever    OpenAI
 cron)                     + Evolution)   Anthropic)
```

## Project Structure

```
alex/
├── agent.py              # Agent core + ChatResponse + notifications
├── config.py             # Config from .env
├── cron.py               # APScheduler-backed CronManager
├── callbacks.py          # LangChain callback → DisplayEvent bridge
├── display.py            # Rich renderer + ThinkingDisplay
├── tui.py                # Textual TUI application
├── llm/                  # LLM factory layer
├── memory/               # Abstract memory (BufferMemory)
├── skills/               # Adaptive skill system
├── streaming/            # Stream event definitions
├── tools/                # web_search, web_fetch, time, cron
└── prompts/              # Jinja2 prompt templates
```

## Documentation

- [Architecture Design](docs/design.md)
- [Agent Core](docs/agent.md)
- [TUI Interface](docs/display.md)
- [LLM Factory](docs/llm.md)
- [Memory Layer](docs/memory.md)
- [Streaming](docs/streaming.md)
- [Skills System](docs/skills.md)
- [Configuration](docs/config.md)

## Tech Stack

- **LangChain** + **LangGraph** — Agent framework & graph execution
- **Textual** — Terminal UI framework
- **Rich** — Terminal rendering
- **APScheduler** — Background job scheduling
- **httpx** + **BeautifulSoup4** — Web fetching
- **DuckDuckGo** (ddgs) — Web search
- **Jinja2** — Prompt templating
- **Pydantic** — Data validation

## License

MIT
