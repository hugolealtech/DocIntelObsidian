"""
Persistência dos jobs de conversão em SQLite.

Cada upload vira uma linha na tabela "jobs". O status evolui:
    pendente -> processando -> concluido | erro

WAL mode + busy_timeout permitem que os workers (threads) e o servidor web
acessem o banco concorrentemente sem erros de "database is locked".
"""

import datetime
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("DOCINTEL_DB", "/data/db/docintel.db")

_init_lock = threading.Lock()
_initialized = False


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = get_conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    original_filename TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    upload_path TEXT,
                    status TEXT NOT NULL DEFAULT 'pendente',
                    disciplina TEXT,
                    professora TEXT,
                    aula TEXT,
                    tags TEXT,
                    job_dir TEXT,
                    md_path TEXT,
                    n_images INTEGER DEFAULT 0,
                    reaproveitado INTEGER DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(file_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def create_job(
    job_id: str,
    original_filename: str,
    slug: str,
    file_hash: str,
    upload_path: str,
    disciplina: str,
    professora: str,
    aula: str,
    tags: list,
) -> None:
    now = _now()
    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO jobs (
                id, original_filename, slug, file_hash, upload_path, status,
                disciplina, professora, aula, tags,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'pendente', ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                original_filename,
                slug,
                file_hash,
                upload_path,
                disciplina,
                professora,
                aula,
                json.dumps(tags or []),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_job(job_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    columns = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [job_id]
    conn = get_conn()
    try:
        conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (TypeError, ValueError):
        d["tags"] = []
    return d


def get_job(job_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def find_done_by_hash(file_hash: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE file_hash = ? AND status = 'concluido' "
            "ORDER BY created_at DESC LIMIT 1",
            (file_hash,),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_jobs(limit: int = 500) -> list:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def list_stuck_jobs() -> list:
    """Jobs que ficaram em 'pendente' ou 'processando' de uma execução anterior
    (o container caiu/reiniciou no meio do processamento)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status IN ('pendente', 'processando')"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()
