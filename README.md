# Claude History Navigator

A web UI for browsing, searching, and analyzing your [Claude Code](https://claude.ai/code) session history.

Claude Code stores every conversation in `~/.claude/projects/*/` as JSONL files. This tool gives you a two-panel interface to navigate them, full-text and semantic search, AI-generated context summaries, and a CLI for quick access after `/clear`.

---

## Features

### Navigation & Viewing
- **Two-panel UI** — project tree on the left, conversation viewer on the right
- **Newest-first message order** — most recent messages at the top for fast context recovery
- Collapsible tool calls, thinking blocks, and tool results
- Download session as Markdown, delete sessions from the UI

### Search
- **Full-text search** — SQLite FTS5 across all sessions and all projects
- **Phrase search** — wrap in quotes for exact matches (`"error handling"`)
- **Semantic search** — optional integration with [open-brain](https://github.com/postnikov/open-brain) API
- In-session match navigation (jump between highlighted results)

### AI Context Generation
Structured session summaries powered by any LLM provider.

| Mode | Output |
|------|--------|
| **Short** | 2-3 paragraph executive summary |
| **Declarative** | Bullet-only facts — what was built, problems, decisions, next steps |
| **Full** | Structured Markdown: task overview with lifecycle status, problems, solutions, key decisions, commands & file paths, last task stop-point |
| **Max** | Full + root causes, architectural highlights, open questions, detailed stop-point |
| **Custom** | Your own prompt — write it in a live textarea or load any preset as a starting point |

**Context features:**
- Languages: **EN** / **UA** / **RU** / IT / DE / ES / PT-BR / Auto-detect
  - EN, UA, RU have full native prompt translations — other languages fall back to EN
- **Cached per (session × language × mode)** — no redundant LLM calls
- **Incremental update** — only sends new messages to LLM when cache exists
- Multi-tab view — switch between cached context variants without regenerating

### LLM Providers & Settings
- **Multi-provider** — Google Gemini, Anthropic Claude, OpenAI, DeepSeek
- **Settings panel** (⚙) — manage API keys, default model, and temperature per provider
- Provider change auto-fills the correct default model and temperature
- Default models: `gemini-2.5-flash` · `claude-sonnet-4-6` · `gpt-5.4-mini` · `deepseek-v4-flash`
- Default temperatures tuned for summarization: Google 0.4 · Anthropic 0.3 · OpenAI 0.3 · DeepSeek 0.5

### CLI
- `navigator --last` — instantly show the most recent session and its cached context
- Resume work after `/clear` without re-generating anything

---

## Quick Start

```bash
git clone https://github.com/forecaster-ua/claude-history-navigator.git
cd claude-history-navigator

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# Add at least one LLM API key
cp .env.example .env
# Edit .env

./venv/bin/uvicorn server:app --host 127.0.0.1 --port 8055
```

Open **http://localhost:8055** in your browser.

---

## Configuration

### API Keys

Add keys to `.env` (or paste them directly in the Settings panel):

```env
GOOGLE_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEEPSEEK_API_KEY=sk-...
```

Key resolution order: **vault** (if configured) → **.env** → **environment variables**.

### Default LLM

`llm_config.json` stores the active provider, model, and temperature:

```json
{
  "provider": "google",
  "model": "gemini-2.5-flash",
  "temperature": 0.4,
  "max_input_chars": 80000
}
```

All fields are editable live in the **Settings panel** — no file editing required.

### Sessions Path

The indexer scans `~/.claude/projects/` by default. All subdirectories are indexed and grouped by decoded project path (e.g., `-home-user-myproject` → `/home/user/myproject`).

---

## CLI

```bash
cp navigator-cli.py ~/bin/navigator
chmod +x ~/bin/navigator
```

```bash
# Show most recent session + cached context
navigator --last

# Generate context via LLM and cache it
navigator --last --generate

# Incremental update (only new messages sent to LLM)
navigator --last --update

# Specify language and mode
navigator --last --generate --lang ru --mode full

# List all sessions
navigator --list
```

**Tip:** After `/clear` in Claude Code, run `navigator --last` to restore context instantly from cache.

---

## Using Custom Prompt Mode

1. Open any session and click **◈ Context**
2. Select **Custom** in the Mode row — the popup expands
3. Pick a preset from the dropdown (**Short / Declarative / Full / Max**) and click **Load template**
   - The template loads in the currently selected **Language** (EN/UA/RU get native translations)
4. Edit the prompt freely — it is saved to `localStorage` between sessions
5. Use `{text}` anywhere in your prompt to mark where the conversation content should be inserted
6. Click **◈ Generate**

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

```bash
sudo htpasswd -c /etc/nginx/.htpasswd youruser
sudo nginx -s reload
```

---

## Architecture

```
claude-history/
├── server.py          # FastAPI backend + context prompt library
├── indexer.py         # JSONL parser + SQLite FTS5 indexer
├── llm_client.py      # Multi-provider LLM client (Gemini / Anthropic / OpenAI / DeepSeek)
├── brain_client.py    # open-brain API client (optional)
├── static/
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── favicon.svg
├── llm_config.json    # Active provider/model/temperature config
├── .env               # API keys (not committed)
├── .env.example
├── requirements.txt
└── ecosystem.config.js
```

**Storage:** SQLite (`claude_history.db`) with WAL mode.
Tables: `sessions`, `sessions_fts` (FTS5), `session_contexts` (context cache per session × language × mode).

**Indexer:** Scans all JSONL files on startup, then every 60 seconds. Incremental — only re-indexes files modified since last scan.

**Context API endpoints:**
- `GET /api/sessions/{id}/context?lang=X&mode=Y` — fetch cached context
- `POST /api/sessions/{id}/context` — generate (supports `custom_prompt` field)
- `POST /api/sessions/{id}/context/update` — incremental update
- `GET /api/context/template?mode=X&lang=Y` — get localized prompt template

---

## Brain API (Optional)

If you run [open-brain](https://github.com/postnikov/open-brain) on `localhost:3100`, semantic search and "Save to Brain" features activate automatically. Text search and context generation work without it.

---

## Roadmap

### i18n — Localized prompt templates
Custom mode loads prompt templates in the selected UI language. Currently **EN, UA, RU** have full native translations; IT, DE, ES, PT-BR fall back to English.

Planned:
- [ ] Native templates for IT, DE, ES, PT-BR
- [ ] CJK support: Simplified Chinese (ZH), Japanese (JA), Korean (KO)
- [ ] Auto-detect language from conversation and pre-select the matching template

> Adding a new language is a single dict entry in `server.py` (`CONTEXT_MODE_PROMPTS_I18N`). Contributions welcome.

### Other planned features
- [ ] Export context to Obsidian / Notion
- [ ] Session tagging and bookmarks
- [ ] Side-by-side diff view for context versions
- [ ] Public demo mode (read-only, no API keys required)

---

## License

MIT
