import os, time, secrets, hashlib, hmac, httpx, json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql+psycopg://", 1)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE, name TEXT, password_hash TEXT, role TEXT DEFAULT 'admin', active BOOLEAN DEFAULT TRUE, created_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id BIGINT REFERENCES users(id), expires_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS applications (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, direction TEXT, age TEXT, preferred_time TEXT, experience TEXT, comment TEXT, status TEXT DEFAULT 'new', admin_note TEXT, created_at BIGINT, updated_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS clients (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, status TEXT DEFAULT 'active', note TEXT, created_at BIGINT, updated_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS site_schedule (id BIGSERIAL PRIMARY KEY, day_name TEXT, start_time TEXT, end_time TEXT, title TEXT, info TEXT, sort_order INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS site_prices (id BIGSERIAL PRIMARY KEY, title TEXT, kicker TEXT, rows_json TEXT, sort_order INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS trainers (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, direction TEXT, note TEXT, active BOOLEAN DEFAULT TRUE)",
    "CREATE TABLE IF NOT EXISTS subscriptions (id BIGSERIAL PRIMARY KEY, client_id BIGINT REFERENCES clients(id), name TEXT, lessons_left INTEGER, ends_at DATE, status TEXT DEFAULT 'active', note TEXT)",
    "CREATE TABLE IF NOT EXISTS payments (id BIGSERIAL PRIMARY KEY, client_id BIGINT REFERENCES clients(id), amount NUMERIC(10,2), payment_date DATE, method TEXT, note TEXT)",
    "CREATE TABLE IF NOT EXISTS trainings (id BIGSERIAL PRIMARY KEY, title TEXT, trainer_id BIGINT, training_date DATE, start_time TIME, end_time TIME, note TEXT)",
    "CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, user_id BIGINT, action TEXT, entity TEXT, entity_id BIGINT, created_at BIGINT)"
]

async def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT: return
    async with httpx.AsyncClient() as client:
        try: await client.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"})
        except: pass

@asynccontextmanager
async def lifespan(app):
    with engine.begin() as conn:
        for s in SCHEMA: conn.execute(text(s))
        if conn.execute(text("SELECT count(*) FROM site_schedule")).scalar() == 0:
            sch = [('ПН','17:30','19:00','Тайский бокс','4+'),('ВТ','19:45','21:15','Бокс','Групповая')]
            for d in sch: conn.execute(text("INSERT INTO site_schedule (day_name,start_time,end_time,title,info) VALUES (:d,:s,:e,:t,:i)"), {"d":d[0],"s":d[1],"e":d[2],"t":d[3],"i":d[4]})
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def now(): return int(time.time())
def get_user(request: Request):
    t = request.cookies.get("bratica_session")
    if not t: raise HTTPException(401)
    with engine.connect() as conn:
        u = conn.execute(text("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=:t AND s.expires_at>:n"), {"t":t,"n":now()}).mappings().first()
    if not u: raise HTTPException(401)
    return dict(u)

@app.get("/api/public/content")
async def get_content():
    with engine.connect() as conn:
        return {
            "schedule": [dict(r) for r in conn.execute(text("SELECT * FROM site_schedule ORDER BY sort_order, start_time")).mappings().all()],
            "prices": [dict(r) for r in conn.execute(text("SELECT * FROM site_prices ORDER BY sort_order")).mappings().all()]
        }

@app.post("/api/applications")
async def create_app(data: dict):
    with engine.begin() as conn:
        res = conn.execute(text("INSERT INTO applications (name, phone, direction, age, preferred_time, experience, comment, created_at, updated_at) VALUES (:n,:p,:d,:a,:pt,:e,:c,:t,:t) RETURNING id"), 
            {"n":data.get("name"), "p":data.get("phone"), "d":data.get("direction"), "a":data.get("age",""), "pt":data.get("preferred_time",""), "e":data.get("experience",""), "c":data.get("comment",""), "t":now()}).first()
    
    msg = f"🔔 <b>НОВАЯ ЗАЯВКА №{res.id}</b>\n👤 {data.get('name')}\n📞 {data.get('phone')}\n🥊 {data.get('direction')}\n💬 {data.get('comment','-')}"
    await send_tg(msg)
    return {"ok":True, "id":res.id}

@app.get("/api/admin/dashboard")
async def admin_dash(u=Depends(get_user)):
    with engine.connect() as conn: 
        return {
            "applications_new": conn.execute(text("SELECT count(*) FROM applications WHERE status='new'")).scalar(),
            "clients_active": conn.execute(text("SELECT count(*) FROM clients WHERE status='active'")).scalar(),
            "subscriptions_active": conn.execute(text("SELECT count(*) FROM subscriptions WHERE status='active'")).scalar(),
            "visits_today": 0
        }

@app.get("/api/admin/{table}")
async def admin_list(table: str, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "trainings"]
    if table not in allowed: raise HTTPException(400)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id DESC")).mappings().all()]

@app.patch("/api/admin/{table}/{id}")
async def admin_patch(table: str, id: int, data: dict, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "trainings"]
    if table not in allowed: raise HTTPException(400)
    sets = ", ".join([f"{k}=:{k}" for k in data.keys()])
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE {table} SET {sets} WHERE id=:id"), {**data, "id": id})
    return {"ok":True}

@app.post("/api/admin/applications/{id}/convert")
async def convert_app(id: int, u=Depends(get_user)):
    with engine.begin() as conn:
        a = conn.execute(text("SELECT * FROM applications WHERE id=:id"), {"id":id}).mappings().first()
        res = conn.execute(text("INSERT INTO clients (name, phone, created_at, updated_at) VALUES (:n,:p,:t,:t) RETURNING id"), {"n":a['name'], "p":a['phone'], "t":now()}).first()
        conn.execute(text("UPDATE applications SET status='completed' WHERE id=:id"), {"id":id})
    return {"ok":True, "client_id": res.id}

@app.get("/", response_class=HTMLResponse)
async def pg_home(): return (BASE/"index.html").read_text(encoding="utf-8")
@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return (BASE/"admin.html").read_text(encoding="utf-8")
@app.get("/admin/login", response_class=HTMLResponse)
async def pg_login(): return (BASE/"admin_login.html").read_text(encoding="utf-8")
