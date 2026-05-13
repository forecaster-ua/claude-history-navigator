# Claude History Navigator

A web UI for browsing, searching, and analyzing your [Claude Code](https://claude.ai/code) session history.

Claude Code stores every conversation in `~/.claude/projects/*/` as JSONL files. This tool gives you a two-panel interface to navigate them, full-text and semantic search, AI-generated context summaries, and a CLI for quick access after `/clear`.

![Claude History Navigator](https://raw.githubusercontent.com/forecaster-ua/claude-history-navigator/main/static/favicon.svg)

---

## Features

- **Two-panel UI** — project tree on the left, conversation viewer on the right
- **Full-text search** — SQLite FTS5 across all sessions and all projects
- **Semantic search** — optional integration with [open-brain](https://github.com/forecaster-ua/open-brain) API
- **AI Context generation** — structured summaries via any LLM provider
  - Languages: EN / **UA** / RU / IT / DE / ES / PT-BR / Auto-detect
  - Modes: Short / Declarative / Full / Max
  - **Cached per (session × language × mode)** — no redundant LLM calls
  - **Incremental update** — only sends new messages to LLM, not the full session
- **Multi-provider LLM** — Google Gemini, Anthropic Claude, OpenAI, DeepSeek
- **Settings panel** — manage API keys and model selection in the UI
- **Session actions** — download as Markdown, delete, save selections to Brain
- **CLI tool** — `navigator --last` to resume context after `/clear`
- **Multi-project indexing** — all `~/.claude/projects/*/` directories, grouped by project path
- Dark theme, collapsible tool calls, thinking blocks

---

## Requirements

- Python 3.10+
- SQLite 3.35+ (ships with Python)
- [PM2](https://pm2.keymetrics.io/) (optional, for production)
- Nginx (optional, for reverse proxy with auth)

---

## Quick Start

```bash
git clone https://github.com/forecaster-ua/claude-history-navigator.git
cd claude-history-navigator

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Configure at least one LLM provider (for context generation)
cp .env.example .env
# Edit .env and add your API key

./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8055
```

Open http://localhost:8055 in your browser.

---

## Configuration

### API Keys

Copy `.env.example` to `.env` and fill in the keys you have:

```env
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=AIza...
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
```

You can also set keys through the **Settings panel** (⚙ button in the header) without editing files.

Key resolution order: **vault** (if configured) → **.env** → **environment variables**.

### LLM Provider

Edit `llm_config.json` to set the default provider and model:

```json
{
  "provider": "google",
  "model": "gemini-2.5-flash",
  "max_input_chars": 80000
}
```

Or change it live in the Settings panel.

### Sessions Path

By default, the indexer scans `~/.claude/projects/`. All subdirectories are indexed and grouped by decoded project path (e.g., `-home-user-myproject` → `/home/user/myproject`).

---

## CLI

Install the CLI by copying the script to your PATH:

```bash
cp navigator-cli.py ~/bin/navigator
chmod +x ~/bin/navigator
```

Usage:

```bash
# Show most recent session info + cached context
navigator --last

# Generate context via LLM (saved to cache)
navigator --last --generate

# Update context with new messages only (incremental)
navigator --last --update

# Specify language and mode
navigator --last --generate --lang ru --mode short

# List all sessions
navigator --list
```

**Tip:** After `/clear` in Claude Code, run `navigator --last` to instantly restore context from cache without re-generating.

---

## Production Deployment

### PM2

```bash
pm2 start ecosystem.config.js
pm2 save
```

### Nginx + Basic Auth

```nginx
location /claude-history/ {
    auth_basic "Claude History Navigator";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8055/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_read_timeout 120;
}
```

Create credentials:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd youruser
sudo nginx -s reload
```

---

## Brain API (Optional)

If you run [open-brain](https://github.com/forecaster-ua/open-brain) on `localhost:3100`, semantic search and "Save to Brain" features become available automatically.

The Brain API is **not required** — text search and context generation work without it.

---

## Architecture

```
claude-history/
├── server.py          # FastAPI backend
├── indexer.py         # JSONL parser + SQLite FTS5 indexer
├── llm_client.py      # Multi-provider LLM client
├── brain_client.py    # Brain API client (optional)
├── static/
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── favicon.svg
├── llm_config.json    # Active provider/model config
├── .env               # API keys (not committed)
├── .env.example
├── requirements.txt
└── ecosystem.config.js
```

**Storage:** SQLite (`claude_history.db`) with WAL mode.  
Tables: `sessions`, `sessions_fts` (FTS5), `session_contexts` (context cache).

**Indexer:** Scans all JSONL files on startup, then every 60 seconds. Incremental — only re-indexes files modified since last scan.

**Thread safety:** Single write connection with `threading.Lock`, fresh read connections per request.

---

## Context Modes

| Mode | Description |
|------|-------------|
| **Short** | 2-3 paragraph executive summary |
| **Declarative** | Bullet points only — facts, decisions, next steps |
| **Full** | Structured Markdown with 6 sections |
| **Max** | Full + code highlights + architectural details |

Each `(session, language, mode)` combination is cached separately. Regenerating EN/full doesn't touch your RU/short cache.

---

## License

MIT
