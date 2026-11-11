import os, time, secrets, hashlib, hmac, json, threading, urllib.request
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
STATIC_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SESSION_DAYS = 30
SHORT_SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SESSION_SECONDS = SESSION_DAYS * 24 * 60 * 60
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() == "true"
STATUSES = {"new", "contacted", "booked", "completed", "cancelled"}
ATTENDANCE_STATUSES = {"present", "absent", "cancelled", "trial"}

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not configured. Configure PostgreSQL DATABASE_URL in Render Environment.")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://") and "+psycopg" not in DATABASE_URL:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine: Engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300)

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL DEFAULT '',password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'admin',active BOOLEAN NOT NULL DEFAULT TRUE,created_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS applications (id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL,direction TEXT NOT NULL,age TEXT DEFAULT '',preferred_time TEXT DEFAULT '',experience TEXT DEFAULT '',comment TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'new',admin_note TEXT DEFAULT '',created_at BIGINT NOT NULL,updated_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS directions (id BIGSERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,active BOOLEAN NOT NULL DEFAULT TRUE)""",
    """CREATE TABLE IF NOT EXISTS trainers (id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,phone TEXT DEFAULT '',direction TEXT DEFAULT '',note TEXT DEFAULT '',active BOOLEAN NOT NULL DEFAULT TRUE)""",
    """CREATE TABLE IF NOT EXISTS clients (id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL,email TEXT DEFAULT '',birth_date TEXT DEFAULT '',age TEXT DEFAULT '',note TEXT DEFAULT '',status TEXT NOT NULL DEFAULT 'active',source_application_id BIGINT REFERENCES applications(id) ON DELETE SET NULL,created_at BIGINT NOT NULL,updated_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS subscription_types (id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,direction TEXT DEFAULT '',price NUMERIC(12,2) NOT NULL DEFAULT 0,lessons INTEGER,duration_days INTEGER,freeze_days INTEGER DEFAULT 0,active BOOLEAN NOT NULL DEFAULT TRUE)""",
    """CREATE TABLE IF NOT EXISTS subscriptions (id BIGSERIAL PRIMARY KEY,client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,type_id BIGINT REFERENCES subscription_types(id) ON DELETE SET NULL,name TEXT NOT NULL,price NUMERIC(12,2) NOT NULL DEFAULT 0,lessons_total INTEGER,lessons_left INTEGER,starts_at DATE NOT NULL,ends_at DATE NOT NULL,status TEXT NOT NULL DEFAULT 'active',frozen_until DATE,note TEXT DEFAULT '',created_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS trainings (id BIGSERIAL PRIMARY KEY,title TEXT NOT NULL,direction TEXT DEFAULT '',trainer_id BIGINT REFERENCES trainers(id) ON DELETE SET NULL,training_date DATE NOT NULL,start_time TIME NOT NULL,end_time TIME NOT NULL,room TEXT DEFAULT '',capacity INTEGER NOT NULL DEFAULT 0,note TEXT DEFAULT '',active BOOLEAN NOT NULL DEFAULT TRUE)""",
    """CREATE TABLE IF NOT EXISTS attendance (id BIGSERIAL PRIMARY KEY,training_id BIGINT NOT NULL REFERENCES trainings(id) ON DELETE CASCADE,client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,status TEXT NOT NULL DEFAULT 'present',note TEXT DEFAULT '',created_at BIGINT NOT NULL,UNIQUE(training_id,client_id))""",
    """CREATE TABLE IF NOT EXISTS payments (id BIGSERIAL PRIMARY KEY,client_id BIGINT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,amount NUMERIC(12,2) NOT NULL,payment_date DATE NOT NULL,method TEXT NOT NULL DEFAULT 'cash',note TEXT DEFAULT '',created_at BIGINT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS audit_log (id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,action TEXT NOT NULL,entity TEXT NOT NULL,entity_id BIGINT,created_at BIGINT NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS site_schedule (id BIGSERIAL PRIMARY KEY, day_name TEXT, start_time TEXT, end_time TEXT, title TEXT, info TEXT, sort_order INTEGER DEFAULT 0)",
    "CREATE TABLE IF NOT EXISTS site_prices (id BIGSERIAL PRIMARY KEY, title TEXT, kicker TEXT DEFAULT '', rows_json TEXT DEFAULT '[]', is_secondary BOOLEAN DEFAULT FALSE, sort_order INTEGER DEFAULT 0)",
]

MIGRATIONS = [
    "ALTER TABLE site_prices ADD COLUMN IF NOT EXISTS kicker TEXT DEFAULT ''",
    "ALTER TABLE site_prices ADD COLUMN IF NOT EXISTS rows_json TEXT DEFAULT '[]'",
    "ALTER TABLE site_prices ADD COLUMN IF NOT EXISTS is_secondary BOOLEAN DEFAULT FALSE",
    "ALTER TABLE site_prices ADD COLUMN IF NOT EXISTS sort_order INTEGER DEFAULT 0",
]

def bootstrap_site(conn):
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
        for statement in SCHEMA:
            conn.execute(text(statement))
        for m in MIGRATIONS:
            conn.execute(text(m))
        for name in ["Тайский бокс", "Бокс", "Киокусинкай-каратэ", "Йога", "BRATICA Transform"]:
            conn.execute(text("INSERT INTO directions(name) VALUES(:name) ON CONFLICT(name) DO NOTHING"), {"name": name})
        bootstrap_site(conn)
    bootstrap_user()
    yield
    engine.dispose()

app = FastAPI(title="BRATICA CRM", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

def now(): return int(time.time())

def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210000)
    return salt.hex() + ":" + digest.hex()

def password_ok(password, stored):
    try:
        salt, digest = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 210000).hex()
        return hmac.compare_digest(actual, digest)
    except Exception:
        return False

def bootstrap_user():
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not email or not password: return
    with engine.begin() as conn:
        found = conn.execute(text("SELECT id FROM users WHERE email=:email"), {"email": email}).first()
        if not found:
            conn.execute(text("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(:email,:name,:hash,'owner',:created)"),
                         {"email": email, "name": "Владелец", "hash": password_hash(password), "created": now()})

def get_user(request: Request):
    token = request.cookies.get("bratica_session")
    if not token: raise HTTPException(401, "Требуется авторизация")
    with engine.connect() as conn:
        row = conn.execute(text("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=:token AND s.expires_at>:now AND u.active=TRUE"),
                           {"token": token, "now": now()}).mappings().first()
    if not row: raise HTTPException(401, "Сессия истекла")
    return dict(row)

def roles(*allowed):
    def dep(user=Depends(get_user)):
        if user["role"] not in allowed: raise HTTPException(403, "Недостаточно прав")
        return user
    return dep

def audit(user, action, entity, entity_id=None):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_log(user_id,action,entity,entity_id,created_at) VALUES(:uid,:action,:entity,:eid,:created)"),
                     {"uid": user["id"], "action": action, "entity": entity, "eid": entity_id, "created": now()})

def fetch_one(conn, query, params=None): return conn.execute(text(query), params or {}).mappings().first()
def fetch_all(conn, query, params=None): return [dict(r) for r in conn.execute(text(query), params or {}).mappings().all()]

def ensure_exists(conn, table, ident):
    allowed = {"clients", "applications", "trainers", "subscriptions", "trainings", "payments", "site_schedule", "site_prices"}
    if table not in allowed: raise HTTPException(500, "Invalid table")
    if not fetch_one(conn, f"SELECT id FROM {table} WHERE id=:id", {"id": ident}): raise HTTPException(404, "Запись не найдена")

class PublicApplication(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")
    name: str = Field(min_length=2, max_length=80)
    phone: str = Field(min_length=6, max_length=30)
    direction: str = Field(min_length=2, max_length=80)
    age: Optional[str] = Field("", max_length=10)
    preferred_time: Optional[str] = Field("", max_length=80)
    experience: Optional[str] = Field("", max_length=80)
    comment: Optional[str] = Field("", max_length=1000)
    website: Optional[str] = Field("", max_length=200)

class ClientIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = Field(min_length=6, max_length=30)
    email: str = ""
    birth_date: str = ""
    age: str = ""
    note: str = ""
    status: str = "active"

class ApplicationPatch(BaseModel):
    status: Optional[str] = None
    admin_note: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None
    direction: Optional[str] = None
    age: Optional[str] = None
    preferred_time: Optional[str] = None
    experience: Optional[str] = None
    comment: Optional[str] = None

class TrainerIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str = ""
    direction: str = ""
    note: str = ""

class SubscriptionIn(BaseModel):
    client_id: int
    type_id: Optional[int] = None
    name: str = Field(min_length=2, max_length=120)
    price: float = Field(default=0, ge=0)
    lessons_total: Optional[int] = Field(default=None, ge=0)
    lessons_left: Optional[int] = Field(default=None, ge=0)
    starts_at: str
    ends_at: str
    note: str = ""

class SubscriptionPatch(BaseModel):
    name: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    lessons_total: Optional[int] = Field(default=None, ge=0)
    lessons_left: Optional[int] = Field(default=None, ge=0)
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    status: Optional[str] = None
    note: Optional[str] = None

class PaymentIn(BaseModel):
    client_id: int
    subscription_id: Optional[int] = None
    amount: float = Field(gt=0)
    payment_date: str
    method: str = "cash"
    note: str = ""

class PaymentPatch(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    payment_date: Optional[str] = None
    method: Optional[str] = None
    note: Optional[str] = None

class TrainingIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    direction: str = ""
    trainer_id: Optional[int] = None
    training_date: str
    start_time: str
    end_time: str
    room: str = ""
    capacity: int = Field(default=0, ge=0)
    note: str = ""

class TrainingPatch(BaseModel):
    title: Optional[str] = Field(default=None, min_length=2, max_length=120)
    direction: Optional[str] = None
    trainer_id: Optional[int] = None
    training_date: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    room: Optional[str] = None
    capacity: Optional[int] = Field(default=None, ge=0)
    note: Optional[str] = None

class AttendanceIn(BaseModel):
    training_id: int
    client_id: int
    status: str = "present"
    note: str = ""

class ScheduleIn(BaseModel):
    day_name: str
    start_time: str
    end_time: str = ""
    title: str
    info: str = ""

class PriceIn(BaseModel):
    title: str
    kicker: str
    rows_json: str
    is_secondary: bool = False

@app.get("/", response_class=HTMLResponse)
async def home():
    f = BASE / "index.html"
    if not f.exists(): raise HTTPException(404, "index.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.get("/health")
async def health():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True, "service": "bratica", "database": "postgresql"}
    except Exception as exc:
        raise HTTPException(503, "Database unavailable") from exc

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    f = BASE / "admin_login.html"
    if not f.exists(): raise HTTPException(404, "admin_login.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8").replace("{{ERROR}}", ""))

@app.post("/admin/login")
async def admin_login(username: str = Form(...), password: str = Form(...)):
    with engine.connect() as conn:
        user = conn.execute(text("SELECT * FROM users WHERE email=:e"), {"e": username.strip().lower()}).mappings().first()

    if not user or not password_ok(password, user["password_hash"]):
        return RedirectResponse(url="/admin/login?error=1", status_code=303)

    token = secrets.token_hex(32)
    expires = now() + 86400 * 7
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sessions (token, user_id, expires_at) VALUES (:t, :u, :e)"),
                     {"t": token, "u": user['id'], "e": expires})

    response = RedirectResponse(url="/admin", status_code=303)
    response.set_cookie(key="bratica_session", value=token, httponly=True, max_age=86400 * 7, samesite="lax")
    return response

@app.post("/admin/logout")
async def logout(request: Request):
    token = request.cookies.get("bratica_session")
    if token:
        with engine.begin() as conn: conn.execute(text("DELETE FROM sessions WHERE token=:token"), {"token": token})
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("bratica_session")
    return response

@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    try:
        get_user(request)
    except HTTPException:
        return RedirectResponse("/admin/login", status_code=303)
    f = BASE / "admin.html"
    if not f.exists(): raise HTTPException(404, "admin.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.post("/api/applications", status_code=201)
async def create_application(data: PublicApplication):
    if data.website: return {"ok": True, "id": 0}
    if sum(ch.isdigit() for ch in data.phone) < 10: raise HTTPException(422, "Некорректный телефон")
    with engine.begin() as conn:
        row = conn.execute(text(
            """INSERT INTO applications(name,phone,direction,age,preferred_time,experience,comment,created_at,updated_at) VALUES(:name,:phone,:direction,:age,:time,:experience,:comment,:created,:updated) RETURNING id"""),
                           {"name": data.name, "phone": data.phone, "direction": data.direction, "age": data.age or "",
                            "time": data.preferred_time or "", "experience": data.experience or "",
                            "comment": data.comment or "", "created": now(), "updated": now()}).first()
    return {"ok": True, "id": row.id, "status": "new"}

@app.get("/api/public/content")
async def get_content():
    with engine.connect() as conn:
        return {
            "schedule": [dict(r) for r in conn.execute(text("SELECT * FROM site_schedule ORDER BY sort_order, start_time")).mappings().all()],
            "prices": [dict(r) for r in conn.execute(text("SELECT * FROM site_prices ORDER BY sort_order")).mappings().all()]
        }

@app.get("/api/admin/me")
async def me(user=Depends(get_user)): return {k: user[k] for k in ("id", "email", "name", "role")}

@app.get("/api/admin/dashboard")
async def dashboard(user=Depends(get_user)):
    with engine.connect() as c:
        return {"applications_new": c.execute(text("SELECT count(*) FROM applications WHERE status='new'")).scalar(),
                "clients_active": c.execute(text("SELECT count(*) FROM clients WHERE status='active'")).scalar(),
                "subscriptions_active": c.execute(text("SELECT count(*) FROM subscriptions WHERE status='active' AND ends_at>=CURRENT_DATE")).scalar(),
                "visits_today": c.execute(text("SELECT count(*) FROM attendance a JOIN trainings t ON t.id=a.training_id WHERE a.status='present' AND t.training_date=CURRENT_DATE")).scalar(),
                "expiring": c.execute(text("SELECT count(*) FROM subscriptions WHERE status='active' AND ends_at BETWEEN CURRENT_DATE AND CURRENT_DATE+7")).scalar()}

@app.get("/api/admin/applications")
async def applications(status: str = "all", q: str = "", user=Depends(get_user)):
    if status != "all" and status not in STATUSES: raise HTTPException(400, "Недопустимый статус")
    conditions = []; p = {}
    if status != "all": conditions.append("status=:status"); p["status"] = status
    if q.strip(): conditions.append("(name ILIKE :q OR phone ILIKE :q OR direction ILIKE :q)"); p["q"] = "%" + q.strip() + "%"
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with engine.connect() as c:
        return fetch_all(c, "SELECT * FROM applications" + where + " ORDER BY created_at DESC", p)

@app.patch("/api/admin/applications/{ident}")
async def application_patch(ident: int, data: ApplicationPatch, user=Depends(get_user)):
    if data.status is not None and data.status not in STATUSES:
        raise HTTPException(status_code=422, detail="Недопустимый статус")
    fields = {"name": data.name, "phone": data.phone, "direction": data.direction, "age": data.age,
              "preferred_time": data.preferred_time, "experience": data.experience, "comment": data.comment,
              "status": data.status, "admin_note": data.admin_note}
    sets = []; params = {"id": ident, "updated": now()}
    for k, v in fields.items():
        if v is not None:
            if k in {"name", "direction"} and not str(v).strip():
                raise HTTPException(status_code=422, detail=f"Поле {k} не может быть пустым")
            if k == "phone" and sum(ch.isdigit() for ch in str(v)) < 10:
                raise HTTPException(status_code=422, detail="Некорректный телефон")
            sets.append(f"{k} = :{k}")
            params[k] = v.strip() if isinstance(v, str) else v
    if not sets: raise HTTPException(status_code=400, detail="Нет данных для обновления")
    sets.append("updated_at = :updated")
    with engine.begin() as conn:
        res = conn.execute(text("UPDATE applications SET " + ", ".join(sets) + " WHERE id = :id"), params)
        if res.rowcount == 0: raise HTTPException(status_code=404, detail="Заявка не найдена")
    audit(user, "update", "application", ident)
    return {"ok": True, "id": ident}

@app.delete("/api/admin/applications/{ident}")
async def application_delete(ident: int, user=Depends(get_user)):
    with engine.begin() as conn:
        res = conn.execute(text("DELETE FROM applications WHERE id = :id"), {"id": ident})
        if res.rowcount == 0: raise HTTPException(status_code=404, detail="Заявка не найдена")
    audit(user, "delete", "application", ident)
    return {"ok": True}

@app.post("/api/admin/applications/{ident}/convert")
async def convert_application(ident: int, user=Depends(get_user)):
    with engine.begin() as c:
        a = fetch_one(c, "SELECT * FROM applications WHERE id=:id", {"id": ident})
        if not a: raise HTTPException(404, "Заявка не найдена")
        existing = fetch_one(c, "SELECT id FROM clients WHERE source_application_id=:id", {"id": ident})
        if existing: return {"ok": True, "client_id": existing["id"]}
        row = c.execute(text(
            "INSERT INTO clients(name,phone,age,note,status,source_application_id,created_at,updated_at) VALUES(:name,:phone,:age,:note,'active',:source,:created,:updated) RETURNING id"),
                        {"name": a["name"], "phone": a["phone"], "age": a["age"], "note": a["comment"], "source": ident,
                         "created": now(), "updated": now()}).first()
        c.execute(text("UPDATE applications SET status='completed',updated_at=:t WHERE id=:id"),
                  {"t": now(), "id": ident})
    audit(user, "convert", "application", ident)
    return {"ok": True, "client_id": row.id}

@app.get("/api/admin/clients")
async def clients(q: str = "", user=Depends(get_user)):
    sql = """SELECT c.*,(SELECT name FROM subscriptions s WHERE s.client_id=c.id AND s.status='active' ORDER BY s.id DESC LIMIT 1) subscription_name,(SELECT lessons_left FROM subscriptions s WHERE s.client_id=c.id AND s.status='active' ORDER BY s.id DESC LIMIT 1) lessons_left FROM clients c"""
    params = {}
    if q.strip(): sql += " WHERE c.name ILIKE :q OR c.phone ILIKE :q"; params["q"] = "%" + q.strip() + "%"
    sql += " ORDER BY c.created_at DESC"
    with engine.connect() as c: return fetch_all(c, sql, params)

@app.get("/api/admin/clients/{ident}")
async def client_detail(ident: int, user=Depends(get_user)):
    with engine.connect() as c:
        client = fetch_one(c, "SELECT * FROM clients WHERE id=:id", {"id": ident})
        if not client: raise HTTPException(404, "Клиент не найден")
        return {"client": dict(client),
                "subscriptions": fetch_all(c, "SELECT * FROM subscriptions WHERE client_id=:id ORDER BY id DESC", {"id": ident}),
                "payments": fetch_all(c, "SELECT * FROM payments WHERE client_id=:id ORDER BY id DESC", {"id": ident}),
                "attendance": fetch_all(c, "SELECT a.*,t.title,t.training_date,t.start_time FROM attendance a JOIN trainings t ON t.id=a.training_id WHERE a.client_id=:id ORDER BY t.training_date DESC,t.start_time DESC", {"id": ident})}

@app.post("/api/admin/clients")
async def client_create(data: ClientIn, user=Depends(get_user)):
    if data.status not in {"active", "frozen", "archived"}: raise HTTPException(422, "Недопустимый статус клиента")
    try:
        with engine.begin() as c:
            row = c.execute(text(
                "INSERT INTO clients(name,phone,email,birth_date,age,note,status,created_at,updated_at) VALUES(:name,:phone,:email,:birth,:age,:note,:status,:created,:updated) RETURNING id"),
                            {"name": data.name, "phone": data.phone, "email": data.email, "birth": data.birth_date,
                             "age": data.age, "note": data.note, "status": data.status, "created": now(),
                             "updated": now()}).first()
    except IntegrityError:
        raise HTTPException(409, "Не удалось создать клиента")
    audit(user, "create", "client", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/clients/{ident}")
async def client_update(ident: int, data: ClientIn, user=Depends(get_user)):
    if data.status not in {"active", "frozen", "archived"}: raise HTTPException(422, "Недопустимый статус клиента")
    with engine.begin() as c:
        result = c.execute(text(
            "UPDATE clients SET name=:name,phone=:phone,email=:email,birth_date=:birth,age=:age,note=:note,status=:status,updated_at=:updated WHERE id=:id"),
                           {"name": data.name, "phone": data.phone, "email": data.email, "birth": data.birth_date,
                            "age": data.age, "note": data.note, "status": data.status, "updated": now(), "id": ident})
        if not result.rowcount: raise HTTPException(404, "Клиент не найден")
    audit(user, "update", "client", ident)
    return {"ok": True}

@app.get("/api/admin/directions")
async def directions(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c, "SELECT * FROM directions WHERE active=TRUE ORDER BY id")

@app.get("/api/public/directions")
async def public_directions():
    with engine.connect() as c: return fetch_all(c, "SELECT id,name FROM directions WHERE active=TRUE ORDER BY id")

@app.get("/api/admin/trainers")
async def trainers(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c, "SELECT * FROM trainers WHERE active=TRUE ORDER BY name")

@app.post("/api/admin/trainers")
async def trainer_create(data: TrainerIn, user=Depends(roles("owner", "admin"))):
    with engine.begin() as c: row = c.execute(text("INSERT INTO trainers(name,phone,direction,note) VALUES(:name,:phone,:direction,:note) RETURNING id"), data.model_dump()).first()
    audit(user, "create", "trainer", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/trainers/{ident}")
async def trainer_update(ident: int, data: TrainerIn, user=Depends(roles("owner", "admin"))):
    with engine.begin() as c:
        res = c.execute(text("UPDATE trainers SET name=:name, phone=:phone, direction=:direction, note=:note WHERE id=:id"), {"id": ident, **data.model_dump()})
        if res.rowcount == 0: raise HTTPException(404, "Тренер не найден")
    audit(user, "update", "trainer", ident)
    return {"ok": True}

@app.get("/api/admin/subscriptions")
async def subscriptions(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c, "SELECT s.*,c.name client_name,c.phone FROM subscriptions s JOIN clients c ON c.id=s.client_id ORDER BY s.id DESC")

@app.post("/api/admin/subscriptions")
async def subscription_create(data: SubscriptionIn, user=Depends(get_user)):
    if data.ends_at < data.starts_at: raise HTTPException(422, "Дата окончания раньше даты начала")
    with engine.begin() as c:
        ensure_exists(c, "clients", data.client_id)
        left = data.lessons_left if data.lessons_left is not None else data.lessons_total
        p = data.model_dump(); p["lessons_left"] = left; p["created_at"] = now()
        row = c.execute(text(
            "INSERT INTO subscriptions(client_id,type_id,name,price,lessons_total,lessons_left,starts_at,ends_at,note,created_at) VALUES(:client_id,:type_id,:name,:price,:lessons_total,:lessons_left,:starts_at,:ends_at,:note,:created_at) RETURNING id"), p).first()
    audit(user, "create", "subscription", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/subscriptions/{ident}")
async def subscription_update(ident: int, data: SubscriptionPatch, user=Depends(get_user)):
    with engine.begin() as c:
        sub = fetch_one(c, "SELECT * FROM subscriptions WHERE id=:id", {"id": ident})
        if not sub: raise HTTPException(404, "Абонемент не найден")
        sets = []; p = {"id": ident}
        for k, v in data.model_dump(exclude_unset=True).items():
            sets.append(f"{k} = :{k}")
            p[k] = v
        if not sets: raise HTTPException(400, "Нет данных для обновления")
        c.execute(text("UPDATE subscriptions SET " + ", ".join(sets) + " WHERE id=:id"), p)
    audit(user, "update", "subscription", ident)
    return {"ok": True}

@app.get("/api/admin/payments")
async def payments(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c, "SELECT p.*,c.name client_name FROM payments p JOIN clients c ON c.id=p.client_id ORDER BY p.id DESC")

@app.post("/api/admin/payments")
async def payment_create(data: PaymentIn, user=Depends(get_user)):
    with engine.begin() as c:
        ensure_exists(c, "clients", data.client_id)
        if data.subscription_id:
            sub = fetch_one(c, "SELECT * FROM subscriptions WHERE id=:id", {"id": data.subscription_id})
            if not sub or sub["client_id"] != data.client_id: raise HTTPException(422, "Абонемент не принадлежит клиенту")
        p = data.model_dump(); p["created_at"] = now()
        row = c.execute(text("INSERT INTO payments(client_id,subscription_id,amount,payment_date,method,note,created_at) VALUES(:client_id,:subscription_id,:amount,:payment_date,:method,:note,:created_at) RETURNING id"), p).first()
    audit(user, "create", "payment", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/payments/{ident}")
async def payment_update(ident: int, data: PaymentPatch, user=Depends(get_user)):
    with engine.begin() as c:
        pay = fetch_one(c, "SELECT * FROM payments WHERE id=:id", {"id": ident})
        if not pay: raise HTTPException(404, "Оплата не найдена")
        sets = []; p = {"id": ident}
        for k, v in data.model_dump(exclude_unset=True).items():
            sets.append(f"{k} = :{k}")
            p[k] = v
        if not sets: raise HTTPException(400, "Нет данных для обновления")
        c.execute(text("UPDATE payments SET " + ", ".join(sets) + " WHERE id=:id"), p)
    audit(user, "update", "payment", ident)
    return {"ok": True}

@app.get("/api/admin/trainings")
async def trainings(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c, "SELECT t.*,tr.name trainer_name FROM trainings t LEFT JOIN trainers tr ON tr.id=t.trainer_id WHERE t.active=TRUE ORDER BY training_date DESC,start_time")

@app.post("/api/admin/trainings")
async def training_create(data: TrainingIn, user=Depends(get_user)):
    if data.end_time <= data.start_time: raise HTTPException(422, "Окончание должно быть позже начала")
    with engine.begin() as c:
        if data.trainer_id: ensure_exists(c, "trainers", data.trainer_id)
        p = data.model_dump()
        row = c.execute(text("INSERT INTO trainings(title,direction,trainer_id,training_date,start_time,end_time,room,capacity,note) VALUES(:title,:direction,:trainer_id,:training_date,:start_time,:end_time,:room,:capacity,:note) RETURNING id"), p).first()
    audit(user, "create", "training", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/trainings/{ident}")
async def training_update(ident: int, data: TrainingPatch, user=Depends(get_user)):
    with engine.begin() as c:
        tr = fetch_one(c, "SELECT * FROM trainings WHERE id=:id", {"id": ident})
        if not tr: raise HTTPException(404, "Тренировка не найдена")
        sets = []; p = {"id": ident}
        for k, v in data.model_dump(exclude_unset=True).items():
            sets.append(f"{k} = :{k}")
            p[k] = v
        if not sets: raise HTTPException(400, "Нет данных для обновления")
        c.execute(text("UPDATE trainings SET " + ", ".join(sets) + " WHERE id=:id"), p)
    audit(user, "update", "training", ident)
    return {"ok": True}

@app.get("/api/admin/site_schedule")
async def get_admin_schedule(user=Depends(get_user)):
    with engine.connect() as c:
        return fetch_all(c, "SELECT * FROM site_schedule ORDER BY sort_order, start_time")

@app.post("/api/admin/site_schedule")
async def create_admin_schedule(data: ScheduleIn, user=Depends(get_user)):
    with engine.begin() as c:
        row = c.execute(text("INSERT INTO site_schedule(day_name,start_time,end_time,title,info) VALUES(:d,:s,:e,:t,:i) RETURNING id"),
                        {"d": data.day_name, "s": data.start_time, "e": data.end_time, "t": data.title, "i": data.info}).first()
    audit(user, "create", "site_schedule", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/site_schedule/{ident}")
async def patch_admin_schedule(ident: int, data: dict, user=Depends(get_user)):
    with engine.begin() as c:
        sets = ", ".join([f"{k}=:{k}" for k in data.keys()])
        c.execute(text(f"UPDATE site_schedule SET {sets} WHERE id=:id"), {**data, "id": ident})
    audit(user, "update", "site_schedule", ident)
    return {"ok": True}

@app.delete("/api/admin/site_schedule/{ident}")
async def delete_admin_schedule(ident: int, user=Depends(get_user)):
    with engine.begin() as c:
        c.execute(text("DELETE FROM site_schedule WHERE id=:id"), {"id": ident})
    audit(user, "delete", "site_schedule", ident)
    return {"ok": True}

@app.get("/api/admin/site_prices")
async def get_admin_prices(user=Depends(get_user)):
    with engine.connect() as c:
        return fetch_all(c, "SELECT * FROM site_prices ORDER BY sort_order")

@app.post("/api/admin/site_prices")
async def create_admin_prices(data: PriceIn, user=Depends(get_user)):
    with engine.begin() as c:
        row = c.execute(text("INSERT INTO site_prices(title,kicker,rows_json,is_secondary) VALUES(:t,:k,:r,:s) RETURNING id"),
                        {"t": data.title, "k": data.kicker, "r": data.rows_json, "s": data.is_secondary}).first()
    audit(user, "create", "site_prices", row.id)
    return {"ok": True, "id": row.id}

@app.patch("/api/admin/site_prices/{ident}")
async def patch_admin_prices(ident: int, data: dict, user=Depends(get_user)):
    with engine.begin() as c:
        sets = ", ".join([f"{k}=:{k}" for k in data.keys()])
        c.execute(text(f"UPDATE site_prices SET {sets} WHERE id=:id"), {**data, "id": ident})
    audit(user, "update", "site_prices", ident)
    return {"ok": True}

@app.delete("/api/admin/site_prices/{ident}")
async def delete_admin_prices(ident: int, user=Depends(get_user)):
    with engine.begin() as c:
        c.execute(text("DELETE FROM site_prices WHERE id=:id"), {"id": ident})
    audit(user, "delete", "site_prices", ident)
    return {"ok": True}

def self_ping():
    while True:
        time.sleep(300)
        try:
            urllib.request.urlopen("https://bratica.onrender.com", timeout=10)
        except Exception:
            pass

threading.Thread(target=self_ping, daemon=True).start()
