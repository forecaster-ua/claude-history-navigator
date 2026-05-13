import json
import os
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
import io
from pydantic import BaseModel

from indexer import (start_background_indexer, open_db, index_all,
                     PROJECTS_ROOT, DB_PATH,
                     get_context_cache, save_context_cache, delete_context_cache)
from brain_client import brain_search, brain_save
from llm_client import generate, get_config, set_config, get_available_providers, set_env_key

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Claude History Browser")


@app.on_event("startup")
async def startup():
    start_background_indexer(interval=30)
    logger.info("Claude History Browser started on port 8055")


@contextmanager
def db():
    """Fresh read-only connection per request."""
    conn = open_db()
    try:
        yield conn
    finally:
        conn.close()


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/sessions")
async def list_sessions(project_dir: Optional[str] = None):
    with db() as conn:
        q = "SELECT id, project_dir, project_path, filename, date, size_bytes, message_count, first_message FROM sessions"
        params = []
        if project_dir:
            q += " WHERE project_dir = ?"
            params.append(project_dir)
        q += " ORDER BY date DESC"
        rows = conn.execute(q, params).fetchall()

    projects: dict = {}
    for row in rows:
        pd = row["project_dir"]
        if pd not in projects:
            projects[pd] = {"project_dir": pd, "project_path": row["project_path"], "sessions": []}
        projects[pd]["sessions"].append({
            "id": row["id"],
            "date": row["date"],
            "size_bytes": row["size_bytes"],
            "message_count": row["message_count"],
            "first_message": row["first_message"],
        })
    return {"projects": list(projects.values())}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT project_dir, filename FROM sessions WHERE id=?", (session_id,)
        ).fetchone()

    if not row:
        raise HTTPException(404, "Session not found")

    filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])
    if not os.path.exists(filepath):
        raise HTTPException(404, "Session file not found on disk")

    messages = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "")
            if not role:
                continue
            messages.append({
                "uuid": obj.get("uuid"),
                "timestamp": obj.get("timestamp"),
                "role": role,
                "content": msg.get("content", ""),
                "toolUseResult": obj.get("toolUseResult"),
            })

    return {"session_id": session_id, "messages": messages}


@app.get("/api/search")
async def search(q: str = Query(..., min_length=1), mode: str = "text", limit: int = 20):
    if mode == "semantic":
        try:
            result = await brain_search(q, limit)
            return {"mode": "semantic", "results": result.get("results", []), "total": result.get("total", 0)}
        except Exception as e:
            raise HTTPException(502, f"Brain API error: {e}")

    # Sanitize FTS5 query — strip special chars that break the parser
    safe_q = " ".join(
        w for w in q.replace('"', '').replace("'", "").replace("*", "").split()
        if w
    )
    if not safe_q:
        return {"mode": "text", "results": [], "total": 0}

    try:
        with db() as conn:
            rows = conn.execute("""
                SELECT s.id, s.project_path, s.date, s.size_bytes, s.message_count,
                       snippet(sessions_fts, 1, '<mark>', '</mark>', '...', 20) as snippet
                FROM sessions_fts
                JOIN sessions s ON s.id = sessions_fts.id
                WHERE sessions_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (safe_q, limit)).fetchall()
        return {"mode": "text", "results": [dict(r) for r in rows], "total": len(rows)}
    except sqlite3.OperationalError as e:
        raise HTTPException(400, f"Search query error: {e}")


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT project_dir, filename FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")

    filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])

    # Delete file
    try:
        if os.path.exists(filepath):
            os.remove(filepath)
    except OSError as e:
        raise HTTPException(500, f"Could not delete file: {e}")

    # Remove from DB
    from indexer import _write_lock, _conn as _wconn
    with _write_lock:
        wconn = _wconn()
        try:
            wconn.execute("DELETE FROM sessions_fts WHERE id=?", (session_id,))
            wconn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            wconn.execute("DELETE FROM session_contexts WHERE session_id=?", (session_id,))
            wconn.commit()
        finally:
            wconn.close()

    return {"ok": True}


@app.get("/api/sessions/{session_id}/download")
async def download_session(session_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT project_dir, filename, date FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")

    filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])
    if not os.path.exists(filepath):
        raise HTTPException(404, "File not found")

    lines_md = [f"# Claude Session: {session_id[:8]}", f"**Date:** {row['date']}",
                f"**Project:** {row['project_dir']}", "", "---", ""]

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")
            ts = obj.get("timestamp", "")[:19].replace("T", " ")

            if not role:
                continue

            role_label = "## 👤 User" if role == "user" else "## 🤖 Claude"
            lines_md.append(f"{role_label}  `{ts}`")
            lines_md.append("")

            if isinstance(content, str):
                lines_md.append(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    t = block.get("type", "")
                    if t == "text" and block.get("text", "").strip():
                        lines_md.append(block["text"])
                    elif t == "tool_use":
                        name = block.get("name", "tool")
                        inp = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
                        lines_md.append(f"```bash\n# Tool: {name}\n{inp}\n```")
                    elif t == "thinking" and block.get("thinking", "").strip():
                        lines_md.append(f"<details><summary>💭 Thinking</summary>\n\n{block['thinking']}\n\n</details>")
            lines_md.append("")
            lines_md.append("---")
            lines_md.append("")

    content_md = "\n".join(lines_md)
    fname = f"claude-session-{session_id[:8]}-{row['date'][:10]}.md"
    return StreamingResponse(
        io.BytesIO(content_md.encode("utf-8")),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'}
    )


# ── Context prompts ───────────────────────────────────────────────────────────

CONTEXT_SYSTEMS = {
    "en": "You are a technical conversation analyst. Create a structured, concise session context in English.",
    "uk": "Ти аналітик технічних переписок. Створи структурований, лаконічний контекст сесії українською мовою.",
    "ru": "Ты аналитик технических переписок. Создай структурированный, лаконичный контекст сессии на русском языке.",
    "it": "Sei un analista di conversazioni tecniche. Crea un contesto strutturato e conciso della sessione in italiano.",
    "de": "Du bist ein technischer Gesprächsanalyst. Erstelle einen strukturierten, prägnanten Sitzungskontext auf Deutsch.",
    "es": "Eres un analista de conversaciones técnicas. Crea un contexto de sesión estructurado y conciso en español.",
}

CONTEXT_MODE_PROMPTS = {
    "short": (
        "Summarize this conversation in 2-3 short paragraphs covering: what was done, "
        "key decisions made, and next steps planned.\n\nCONVERSATION:\n{text}"
    ),
    "declarative": (
        "Extract key facts from this conversation as bullet points only (no prose):\n"
        "• What was built/solved\n• Problems encountered\n• Solutions found\n"
        "• Key decisions\n• Next steps\n\nCONVERSATION:\n{text}"
    ),
    "full": (
        "Analyze the following conversation and create a structured context in Markdown.\n\n"
        "Use these sections:\n"
        "## What we worked on\n"
        "## Problems and challenges\n"
        "## Iterations and solutions\n"
        "## Key decisions\n"
        "## Discussions and ideas\n"
        "## Plans and next steps\n\n"
        "CONVERSATION:\n{text}"
    ),
    "max": (
        "Analyze the following conversation and create a comprehensive context in Markdown.\n\n"
        "Use these sections:\n"
        "## What we worked on\n"
        "## Problems and challenges\n"
        "## Iterations and solutions (include specific approaches tried)\n"
        "## Key decisions (with rationale)\n"
        "## Code and architecture highlights\n"
        "## Discussions and ideas\n"
        "## Plans and next steps\n"
        "## Open questions\n\n"
        "Be thorough — include code patterns, architectural choices, and specific details.\n\n"
        "CONVERSATION:\n{text}"
    ),
}

DELTA_PROMPT = (
    "Existing context summary:\n{existing}\n\n"
    "New messages added since the summary was generated:\n{new_messages}\n\n"
    "Update the context to incorporate the new information. Keep the same structure and language."
)


def detect_lang(text: str) -> str:
    cyrillic = sum(1 for c in text if 'Ѐ' <= c <= 'ӿ')
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    if cyrillic > latin:
        # Ukrainian-specific letters: і, ї, є, ґ
        ukrainian = sum(1 for c in text if c in 'іїєґІЇЄҐ')
        return "uk" if ukrainian > cyrillic * 0.05 else "ru"
    return "en"


def _extract_session_parts(filepath: str, skip: int = 0) -> tuple[list[str], int]:
    """Extract text parts from JSONL. skip=N skips first N messages. Returns (parts, total_count)."""
    parts = []
    total = 0
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") not in ("user", "assistant"):
                continue
            msg = obj.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not role:
                continue
            if isinstance(content, list) and content and isinstance(content[0], dict):
                if content[0].get("type") == "tool_result":
                    continue
            text = ""
            if isinstance(content, str):
                text = content.strip()
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "").strip() + " "
            text = text.strip()
            if text:
                total += 1
                if total > skip:
                    parts.append(f"[{role.upper()}]: {text}")
    return parts, total


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return text[:half] + "\n\n[... middle truncated ...]\n\n" + text[-half:]


@app.get("/api/sessions/{session_id}/context")
async def get_context(session_id: str, lang: str = "en", mode: str = "full"):
    with db() as conn:
        session = conn.execute(
            "SELECT message_count FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not session:
        raise HTTPException(404, "Session not found")

    cached = get_context_cache(session_id, lang, mode)
    if not cached:
        return {"cached": False, "session_message_count": session["message_count"]}

    is_stale = cached["message_count"] < session["message_count"]
    import datetime
    gen_ago = ""
    if cached["generated_at"]:
        delta = datetime.datetime.now().timestamp() - cached["generated_at"]
        if delta < 3600:
            gen_ago = f"{int(delta/60)}m ago"
        elif delta < 86400:
            gen_ago = f"{int(delta/3600)}h ago"
        else:
            gen_ago = f"{int(delta/86400)}d ago"

    return {
        "cached": True,
        "context_text": cached["context_text"],
        "language": cached["language"],
        "mode": cached["mode"],
        "provider": cached["provider"],
        "model": cached["model"],
        "generated_at": cached["generated_at"],
        "generated_ago": gen_ago,
        "message_count": cached["message_count"],
        "session_message_count": session["message_count"],
        "is_stale": is_stale,
    }


class ContextRequest(BaseModel):
    lang: str = "en"
    mode: str = "full"
    provider: Optional[str] = None
    model: Optional[str] = None


@app.post("/api/sessions/{session_id}/context")
async def create_context(session_id: str, req: ContextRequest):
    with db() as conn:
        row = conn.execute(
            "SELECT project_dir, filename, date FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")

    filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])
    if not os.path.exists(filepath):
        raise HTTPException(404, "File not found")

    parts, total_count = _extract_session_parts(filepath)
    full_text = _truncate("\n\n".join(parts), get_config().get("max_input_chars", 80000))

    lang = req.lang
    if lang == "auto":
        lang = detect_lang(full_text)

    mode = req.mode if req.mode in CONTEXT_MODE_PROMPTS else "full"
    system = CONTEXT_SYSTEMS.get(lang, CONTEXT_SYSTEMS["en"])
    prompt = CONTEXT_MODE_PROMPTS[mode].format(text=full_text)

    cfg = get_config()
    use_provider = req.provider or cfg["provider"]
    use_model = req.model or cfg.get("model", "")

    try:
        result = await generate(prompt, system=system, provider=use_provider, model=use_model)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")

    header = f"# Context: {session_id[:8]}\n**Date:** {row['date']}  **Project:** {row['project_dir']}\n\n"
    context_text = header + result

    save_context_cache(session_id, lang, mode, context_text,
                       use_provider, use_model, total_count)

    return {
        "context_text": context_text,
        "language": lang,
        "mode": mode,
        "provider": use_provider,
        "model": use_model,
        "message_count": total_count,
    }


@app.post("/api/sessions/{session_id}/context/update")
async def update_context(session_id: str, req: ContextRequest):
    """Incremental update: only send new messages to LLM."""
    with db() as conn:
        row = conn.execute(
            "SELECT project_dir, filename, date FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(404, "Session not found")

    cached = get_context_cache(session_id, req.lang, req.mode)
    if not cached:
        return await create_context(session_id, req)

    filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])
    new_parts, total_count = _extract_session_parts(filepath, skip=cached["message_count"])

    if not new_parts:
        return {
            "context_text": cached["context_text"],
            "language": req.lang,
            "mode": req.mode,
            "provider": cached["provider"],
            "model": cached["model"],
            "message_count": total_count,
            "updated": False,
        }

    new_text = _truncate("\n\n".join(new_parts), 30000)
    lang = req.lang if req.lang != "auto" else cached["language"]
    system = CONTEXT_SYSTEMS.get(lang, CONTEXT_SYSTEMS["en"])
    prompt = DELTA_PROMPT.format(existing=cached["context_text"], new_messages=new_text)

    cfg = get_config()
    use_provider = req.provider or cfg["provider"]
    use_model = req.model or cfg.get("model", "")

    try:
        result = await generate(prompt, system=system, provider=use_provider, model=use_model)
    except Exception as e:
        raise HTTPException(502, f"LLM error: {e}")

    save_context_cache(session_id, lang, req.mode, result,
                       use_provider, use_model, total_count)

    return {
        "context_text": result,
        "language": lang,
        "mode": req.mode,
        "provider": use_provider,
        "model": use_model,
        "message_count": total_count,
        "updated": True,
    }


@app.get("/api/llm/config")
async def llm_config_get():
    return get_config()


class LLMConfigUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    max_input_chars: int | None = None


@app.put("/api/llm/config")
async def llm_config_update(upd: LLMConfigUpdate):
    updates = {k: v for k, v in upd.model_dump().items() if v is not None}
    set_config(updates)
    return get_config()


@app.get("/api/providers")
async def list_providers():
    return {"providers": get_available_providers()}


class ProviderKeyRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/providers/key")
async def set_provider_key(req: ProviderKeyRequest):
    try:
        set_env_key(req.provider, req.api_key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/brain/search")
async def api_brain_search(q: str = Query(..., min_length=1), limit: int = 10):
    try:
        return await brain_search(q, limit)
    except Exception as e:
        raise HTTPException(502, f"Brain API error: {e}")


class BrainSaveRequest(BaseModel):
    text: str
    session_id: str
    tags: list[str] = []


@app.post("/api/brain/save")
async def api_brain_save(req: BrainSaveRequest):
    result = await brain_save(req.text, req.session_id, req.tags)
    if not result.get("ok"):
        raise HTTPException(502, result.get("error", "Brain save failed"))
    return {"ok": True}


@app.post("/api/index/refresh")
async def refresh_index():
    index_all()
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return {"ok": True, "total_sessions": count}


@app.get("/api/stats")
async def stats():
    with db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        projects = conn.execute("SELECT COUNT(DISTINCT project_dir) FROM sessions").fetchone()[0]
    return {"total_sessions": total, "total_projects": projects}


# ── Static ────────────────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="/home/alexross/claude-history/static"), name="static")


@app.get("/")
async def root():
    return FileResponse("/home/alexross/claude-history/static/index.html")


@app.get("/{path:path}")
async def catch_all(path: str):
    return FileResponse("/home/alexross/claude-history/static/index.html")
