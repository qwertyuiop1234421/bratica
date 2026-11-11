import os, time, secrets, hashlib, hmac, json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql+psycopg://", 1)

engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE, name TEXT, password_hash TEXT, role TEXT DEFAULT 'admin', active BOOLEAN DEFAULT TRUE, created_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id BIGINT REFERENCES users(id), expires_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS applications (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, direction TEXT, age TEXT, preferred_time TEXT, experience TEXT, comment TEXT, status TEXT DEFAULT 'new', admin_note TEXT, created_at BIGINT, updated_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS clients (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, status TEXT DEFAULT 'active', note TEXT, created_at BIGINT, updated_at BIGINT)",
    "CREATE TABLE IF NOT EXISTS site_schedule (id BIGSERIAL PRIMARY KEY, day_name TEXT, start_time TEXT, end_time TEXT, title TEXT, info TEXT, sort_order INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS site_prices (id BIGSERIAL PRIMARY KEY, title TEXT, kicker TEXT, rows_json TEXT, is_secondary BOOLEAN DEFAULT FALSE, sort_order INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS trainers (id BIGSERIAL PRIMARY KEY, name TEXT, phone TEXT, direction TEXT, note TEXT, active BOOLEAN DEFAULT TRUE)",
    "CREATE TABLE IF NOT EXISTS subscriptions (id BIGSERIAL PRIMARY KEY, client_id BIGINT REFERENCES clients(id), name TEXT, lessons_left INTEGER, ends_at DATE, status TEXT DEFAULT 'active', note TEXT)",
    "CREATE TABLE IF NOT EXISTS payments (id BIGSERIAL PRIMARY KEY, client_id BIGINT REFERENCES clients(id), amount NUMERIC(10,2), payment_date DATE, method TEXT, note TEXT)",
    "CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY, user_id BIGINT, action TEXT, entity TEXT, entity_id BIGINT, created_at BIGINT)"
]

def bootstrap_site(conn):
    # Первичная автоматическая набивка актуального расписания BRATICA
    if conn.execute(text("SELECT count(*) FROM site_schedule")).scalar() == 0:
        sch = [
            ('ПН','17:30','19:00','Тайский бокс','Дети 4+ (Группа Б)'),
            ('ПН','19:00','20:30','Киокусинкай-каратэ','Группа Б'),
            ('ВТ','18:30','19:45','Бокс','Группа Б'),
            ('ВТ','19:45','21:15','Тайский бокс','Группа Б'),
            ('ВТ','19:30','20:45','Киокусинкай-каратэ','14+ (Группа М)'),
            ('СР','09:00','10:00','Йога','Группа М'),
            ('СР','17:30','19:00','Тайский бокс','Дети 4+ (Группа Б)'),
            ('СР','19:00','20:30','Киокусинкай-каратэ','Группа Б'),
            ('ЧТ','18:30','19:45','Бокс','Группа Б'),
            ('ЧТ','19:45','21:15','Тайский бокс','Группа Б'),
            ('ЧТ','19:30','21:00','Тайский бокс','Взрослые (Группа Б)'),
            ('ПТ','17:30','19:00','Тайский бокс','Группа Б'),
            ('ПТ','19:00','20:30','Киокусинкай-каратэ','Группа Б'),
            ('ВС','10:00','11:00','Йога','Группа М')
        ]
        for s in sch:
            conn.execute(text("INSERT INTO site_schedule (day_name,start_time,end_time,title,info) VALUES (:d,:s,:e,:t,:i)"), {"d":s[0],"s":s[1],"e":s[2],"t":s[3],"i":s[4]})
    
    # Первичная автоматическая набивка актуального прайса BRATICA
    if conn.execute(text("SELECT count(*) FROM site_prices")).scalar() == 0:
        pr = [
            (
                'Детское посещение',
                'Единоборства · дети',
                json.dumps([['Разовое посещение','400 ₽'],['8 занятий','2 600 ₽'],['12 занятий','3 000 ₽'],['Персональная','1 200 ₽']]),
                False
            ),
            (
                'Взрослое посещение',
                'Единоборства · взрослые',
                json.dumps([['Разовое посещение','400 ₽'],['8 занятий','2 900 ₽'],['12 занятий','3 400 ₽'],['Персональная','1 500 ₽']]),
                True
            ),
            (
                'Йога',
                'Практика и восстановление',
                json.dumps([['4 занятия','3 000 ₽'],['8 занятий','5 500 ₽']]),
                True
            )
        ]
        for p in pr:
            conn.execute(text("INSERT INTO site_prices (title,kicker,rows_json,is_secondary) VALUES (:t,:k,:r,:s)"), {"t":p[0],"k":p[1],"r":p[2],"s":p[3]})

@asynccontextmanager
async def lifespan(app):
    with engine.begin() as conn:
        for s in SCHEMA: conn.execute(text(s))
        bootstrap_site(conn)
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
    return {"ok":True, "id":res.id}

@app.get("/api/admin/dashboard")
async def admin_dash(u=Depends(get_user)):
    with engine.connect() as conn:
        return {
            "applications_new": conn.execute(text("SELECT count(*) FROM applications WHERE status='new'")).scalar(),
            "clients_active": conn.execute(text("SELECT count(*) FROM clients WHERE status='active'")).scalar(),
            "subscriptions_active": conn.execute(text("SELECT count(*) FROM subscriptions WHERE status='active'")).scalar()
        }

@app.get("/api/admin/{table}")
async def admin_list(table: str, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "site_schedule", "site_prices"]
    if table not in allowed: raise HTTPException(400)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(f"SELECT * FROM {table} ORDER BY id DESC")).mappings().all()]

@app.post("/api/admin/{table}")
async def admin_add(table: str, data: dict, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "site_schedule", "site_prices"]
    if table not in allowed: raise HTTPException(400)
    keys = ", ".join(data.keys()); vals = ", ".join([f":{k}" for k in data.keys()])
    with engine.begin() as conn: conn.execute(text(f"INSERT INTO {table} ({keys}) VALUES ({vals})"), data)
    return {"ok":True}

@app.patch("/api/admin/{table}/{id}")
async def admin_patch(table: str, id: int, data: dict, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "site_schedule", "site_prices"]
    if table not in allowed: raise HTTPException(400)
    sets = ", ".join([f"{k}=:{k}" for k in data.keys()])
    with engine.begin() as conn: conn.execute(text(f"UPDATE {table} SET {sets} WHERE id=:id"), {**data, "id": id})
    return {"ok":True}

@app.delete("/api/admin/{table}/{id}")
async def admin_del(table: str, id: int, u=Depends(get_user)):
    allowed = ["applications", "clients", "subscriptions", "payments", "trainers", "site_schedule", "site_prices"]
    if table not in allowed: raise HTTPException(400)
    with engine.begin() as conn: conn.execute(text(f"DELETE FROM {table} WHERE id=:id"), {"id":id})
    return {"ok":True}

@app.get("/", response_class=HTMLResponse)
async def pg_home(): return (BASE/"index.html").read_text(encoding="utf-8")

@app.get("/admin", response_class=HTMLResponse)
async def pg_admin(): return (BASE/"admin.html").read_text(encoding="utf-8")

@app.get("/admin/login", response_class=HTMLResponse)
async def pg_login(): return (BASE/"admin_login.html").read_text(encoding="utf-8")
