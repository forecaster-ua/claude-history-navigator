import sqlite3
import json
import os
import glob
import time
import threading
import logging
from datetime import datetime
from pathlib import Path

DB_PATH = "/home/alexross/claude-history/claude_history.db"
PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

logger = logging.getLogger(__name__)
_write_lock = threading.Lock()


def decode_project_path(dir_name: str) -> str:
    if dir_name.startswith("-"):
        return dir_name.replace("-", "/", 1).replace("-", "/")
    return dir_name


def _conn(readonly=False) -> sqlite3.Connection:
    uri = f"file:{DB_PATH}{'?mode=ro' if readonly else ''}";
    conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def open_db() -> sqlite3.Connection:
    """Read-only connection for API handlers — open fresh, close after use."""
    return _conn(readonly=False)  # WAL allows concurrent reads even without uri ro


def init_db():
    with _write_lock:
        conn = _conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id          TEXT PRIMARY KEY,
                    project_dir TEXT,
                    project_path TEXT,
                    filename    TEXT,
                    date        TEXT,
                    size_bytes  INTEGER,
                    message_count INTEGER,
                    first_message TEXT,
                    indexed_at  REAL
                );

                CREATE TABLE IF NOT EXISTS sessions_text (
                    id      TEXT PRIMARY KEY,
                    content TEXT
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
                USING fts5(id UNINDEXED, content, tokenize='unicode61');

                CREATE TABLE IF NOT EXISTS session_contexts (
                    session_id    TEXT NOT NULL,
                    language      TEXT NOT NULL,
                    mode          TEXT NOT NULL,
                    context_text  TEXT,
                    generated_at  REAL,
                    provider      TEXT,
                    model         TEXT,
                    message_count INTEGER,
                    PRIMARY KEY (session_id, language, mode)
                );
            """)
            conn.commit()
        finally:
            conn.close()


def get_context_cache(session_id: str, lang: str, mode: str) -> dict | None:
    conn = open_db()
    try:
        row = conn.execute(
            "SELECT * FROM session_contexts WHERE session_id=? AND language=? AND mode=?",
            (session_id, lang, mode)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def save_context_cache(session_id: str, lang: str, mode: str, text: str,
                       provider: str, model: str, message_count: int):
    with _write_lock:
        conn = _conn()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO session_contexts
                (session_id, language, mode, context_text, generated_at, provider, model, message_count)
                VALUES (?,?,?,?,?,?,?,?)
            """, (session_id, lang, mode, text, time.time(), provider, model, message_count))
            conn.commit()
        finally:
            conn.close()


def delete_context_cache(session_id: str):
    with _write_lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM session_contexts WHERE session_id=?", (session_id,))
            conn.commit()
        finally:
            conn.close()


def extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t == "text":
                    parts.append(block.get("text", "").strip())
                elif t == "tool_use":
                    parts.append(f"[Tool: {block.get('name', '')}]")
                elif t == "tool_result":
                    inner = block.get("content", "")
                    if isinstance(inner, str):
                        parts.append(inner[:300])
        return " ".join(p for p in parts if p)
    return ""


def parse_jsonl(filepath: str) -> dict:
    messages = []
    session_id = None
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not session_id and obj.get("sessionId"):
                    session_id = obj["sessionId"]
                if obj.get("type") not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                role = msg.get("role", "")
                if not role:
                    continue
                content = msg.get("content", "")
                # skip bare tool results
                if isinstance(content, list) and content and isinstance(content[0], dict):
                    if content[0].get("type") == "tool_result":
                        continue
                text = extract_text(content)
                if text:
                    messages.append({"role": role, "text": text})
    except Exception as e:
        logger.warning(f"Error parsing {filepath}: {e}")

    if not session_id:
        session_id = Path(filepath).stem

    first_message = next((m["text"][:200] for m in messages if m["role"] == "user"), "")
    all_text = " ".join(m["text"] for m in messages)
    return {
        "session_id": session_id,
        "message_count": len(messages),
        "first_message": first_message,
        "all_text": all_text,
    }


def reconcile_stale() -> int:
    """Remove DB entries whose files no longer exist on disk. Returns count removed."""
    removed = 0
    with _write_lock:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id, project_dir, filename FROM sessions"
            ).fetchall()
            stale_ids = []
            for row in rows:
                filepath = os.path.join(PROJECTS_ROOT, row["project_dir"], row["filename"])
                if not os.path.exists(filepath):
                    stale_ids.append(row["id"])
            for sid in stale_ids:
                conn.execute("DELETE FROM sessions_fts WHERE id=?", (sid,))
                conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
                conn.execute("DELETE FROM sessions_text WHERE id=?", (sid,))
                conn.execute("DELETE FROM session_contexts WHERE session_id=?", (sid,))
                removed += 1
            if stale_ids:
                conn.commit()
        finally:
            conn.close()
    if removed:
        logger.info(f"Reconciliation: removed {removed} stale entries")
    return removed


def index_all() -> int:
    """Index new/changed files. Returns count of newly indexed sessions."""
    if not os.path.isdir(PROJECTS_ROOT):
        return 0

    added = 0
    with _write_lock:
        conn = _conn()
        try:
            for project_dir in os.listdir(PROJECTS_ROOT):
                full_dir = os.path.join(PROJECTS_ROOT, project_dir)
                if not os.path.isdir(full_dir):
                    continue
                project_path = decode_project_path(project_dir)

                for jsonl_file in glob.glob(os.path.join(full_dir, "*.jsonl")):
                    try:
                        stat = os.stat(jsonl_file)
                        mtime, size = stat.st_mtime, stat.st_size
                        sid = Path(jsonl_file).stem

                        row = conn.execute(
                            "SELECT indexed_at FROM sessions WHERE id=?", (sid,)
                        ).fetchone()
                        if row and row[0] >= mtime:
                            continue

                        parsed = parse_jsonl(jsonl_file)
                        date = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")

                        conn.execute("""
                            INSERT OR REPLACE INTO sessions
                            (id, project_dir, project_path, filename, date,
                             size_bytes, message_count, first_message, indexed_at)
                            VALUES (?,?,?,?,?,?,?,?,?)
                        """, (
                            parsed["session_id"], project_dir, project_path,
                            os.path.basename(jsonl_file), date, size,
                            parsed["message_count"], parsed["first_message"],
                            time.time()
                        ))

                        conn.execute(
                            "DELETE FROM sessions_fts WHERE id=?", (parsed["session_id"],)
                        )
                        conn.execute(
                            "INSERT INTO sessions_fts(id, content) VALUES (?,?)",
                            (parsed["session_id"], parsed["all_text"])
                        )
                        added += 1

                    except Exception as e:
                        logger.warning(f"Failed to index {jsonl_file}: {e}")

            conn.commit()
        finally:
            conn.close()

    logger.info(f"Indexing complete: {added} new/updated")
    return added


def start_background_indexer(interval: int = 60):
    init_db()
    # Initial index in background so startup is fast
    t = threading.Thread(target=index_all, daemon=True)
    t.start()

    def loop():
        while True:
            time.sleep(interval)
            try:
                index_all()
            except Exception as e:
                logger.error(f"Background indexer error: {e}")

    threading.Thread(target=loop, daemon=True).start()
