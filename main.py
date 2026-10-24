# -*- coding: utf-8 -*-
"""BRATICA Fight Club backend.

Run locally:
  pip install fastapi uvicorn pydantic
  ADMIN_TOKEN="change-me" uvicorn main:app --host 0.0.0.0 --port 10000

Render start command:
  uvicorn main:app --host 0.0.0.0 --port $PORT

Environment:
  ADMIN_TOKEN  - secret for admin application API
  DB_FILE      - optional SQLite path
  KEEP_ALIVE_URL - optional public /health URL; used by the internal pinger
"""
import os
import hmac
import sqlite3
import time
import asyncio
import urllib.request
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, ConfigDict

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = os.getenv("DB_FILE", str(BASE_DIR / "bratica.db"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
PORT = int(os.getenv("PORT", "10000"))
KEEP_ALIVE_URL = os.getenv("KEEP_ALIVE_URL", "").strip()
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL", "420"))
STATUSES = {"new", "contacted", "booked", "completed", "cancelled"}


def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_db()
    with conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            direction TEXT NOT NULL,
            age TEXT DEFAULT '',
            preferred_time TEXT DEFAULT '',
            experience TEXT DEFAULT '',
            comment TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            admin_note TEXT DEFAULT '',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_apps_status_date ON applications(status, created_at DESC)")
    conn.close()


async def keep_alive_worker():
    """Best-effort self-ping. External cron is still recommended on Render Free."""
    if not KEEP_ALIVE_URL:
        return
    await asyncio.sleep(20)
    while True:
        try:
            req = urllib.request.Request(
                KEEP_ALIVE_URL.rstrip("/") + "/health",
                headers={"User-Agent": "BRATICA-KeepAlive/1.0"},
            )
            await asyncio.to_thread(urllib.request.urlopen, req, timeout=12)
        except Exception:
            pass
        await asyncio.sleep(max(120, KEEP_ALIVE_INTERVAL))


@asynccontextmanager
async def lifespan(app):
    init_db()
    task = asyncio.create_task(keep_alive_worker())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="BRATICA Fight Club", version="1.1.0", lifespan=lifespan)


class ApplicationIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=30)
    direction: str = Field(min_length=2, max_length=80)
    age: Optional[str] = Field(default="", max_length=10)
    preferred_time: Optional[str] = Field(default="", max_length=80)
    experience: Optional[str] = Field(default="", max_length=80)
    comment: Optional[str] = Field(default="", max_length=1000)
    website: Optional[str] = Field(default="", max_length=200)


class ApplicationPatch(BaseModel):
    status: Optional[str] = Field(default=None, max_length=20)
    admin_note: Optional[str] = Field(default=None, max_length=1000)


def require_admin(token: Optional[str]):
    if not ADMIN_TOKEN:
        raise HTTPException(503, "Set ADMIN_TOKEN environment variable")
    if not token or not hmac.compare_digest(token, ADMIN_TOKEN):
        raise HTTPException(401, "Unauthorized")


@app.get("/", response_class=HTMLResponse)
async def homepage():
    file = BASE_DIR / "index.html"
    if not file.is_file():
        raise HTTPException(404, "index.html not found")
    return HTMLResponse(file.read_text(encoding="utf-8"))


@app.get("/health")
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "bratica-fight-club", "timestamp": int(time.time())}


@app.post("/api/applications", status_code=201)
async def create_application(body: ApplicationIn):
    if body.website:
        return {"ok": True, "id": 0}
    if sum(ch.isdigit() for ch in body.phone) < 10:
        raise HTTPException(422, "Введите корректный номер телефона")
    now = time.time()
    conn = get_db()
    try:
        with conn:
            cur = conn.execute("""INSERT INTO applications
                (name, phone, direction, age, preferred_time, experience, comment, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                body.name, body.phone, body.direction, body.age or "",
                body.preferred_time or "", body.experience or "", body.comment or "", now, now
            ))
        return {"ok": True, "id": cur.lastrowid, "status": "new"}
    finally:
        conn.close()


@app.get("/api/admin/applications")
async def list_applications(
    status: str = Query("all", max_length=20),
    q: str = Query("", max_length=100),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    x_admin_token: Optional[str] = Header(default=None),
):
    require_admin(x_admin_token)
    if status != "all" and status not in STATUSES:
        raise HTTPException(400, "Invalid status")
    clauses, params = [], []
    if status != "all":
        clauses.append("status=?")
        params.append(status)
    if q.strip():
        clauses.append("(name LIKE ? OR phone LIKE ? OR direction LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 3)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM applications" + where, params).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM applications" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        counts = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, COUNT(*) n FROM applications GROUP BY status"
        )}
        return {"ok": True, "total": total, "counts": counts,
                "applications": [dict(row) for row in rows]}
    finally:
        conn.close()


@app.patch("/api/admin/applications/{application_id}")
async def update_application(application_id: int, body: ApplicationPatch,
                             x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    if body.status is None and body.admin_note is None:
        raise HTTPException(400, "Nothing to update")
    if body.status is not None and body.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    fields, params = [], []
    if body.status is not None:
        fields.append("status=?")
        params.append(body.status)
    if body.admin_note is not None:
        fields.append("admin_note=?")
        params.append(body.admin_note)
    fields.append("updated_at=?")
    params.extend([time.time(), application_id])
    conn = get_db()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE applications SET " + ",".join(fields) + " WHERE id=?", params
            )
        if not cur.rowcount:
            raise HTTPException(404, "Application not found")
        return {"ok": True, "id": application_id}
    finally:
        conn.close()


@app.delete("/api/admin/applications/{application_id}")
async def delete_application(application_id: int,
                             x_admin_token: Optional[str] = Header(default=None)):
    require_admin(x_admin_token)
    conn = get_db()
    try:
        with conn:
            cur = conn.execute("DELETE FROM applications WHERE id=?", (application_id,))
        if not cur.rowcount:
            raise HTTPException(404, "Application not found")
        return {"ok": True, "id": application_id}
    finally:
        conn.close()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
