import os, time, secrets, hashlib, hmac
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
]

@asynccontextmanager
async def lifespan(app):
    with engine.begin() as conn:
        for statement in SCHEMA:
            conn.execute(text(statement))
        for name in ["Тайский бокс", "Бокс", "Киокусинкай-каратэ", "Йога", "BRATICA Transform"]:
            conn.execute(text("INSERT INTO directions(name) VALUES(:name) ON CONFLICT(name) DO NOTHING"), {"name": name})
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
    except Exception: return False

def bootstrap_user():
    email=os.getenv("ADMIN_EMAIL", "").strip().lower(); password=os.getenv("ADMIN_PASSWORD", "")
    if not email or not password: return
    with engine.begin() as conn:
        found=conn.execute(text("SELECT id FROM users WHERE email=:email"), {"email":email}).first()
        if not found:
            conn.execute(text("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(:email,:name,:hash,'owner',:created)"), {"email":email,"name":"Владелец","hash":password_hash(password),"created":now()})

def get_user(request: Request):
    token=request.cookies.get("bratica_session")
    if not token: raise HTTPException(401,"Требуется авторизация")
    with engine.connect() as conn:
        row=conn.execute(text("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=:token AND s.expires_at>:now AND u.active=TRUE"), {"token":token,"now":now()}).mappings().first()
    if not row: raise HTTPException(401,"Сессия истекла")
    return dict(row)
def roles(*allowed):
    def dep(user=Depends(get_user)):
        if user["role"] not in allowed: raise HTTPException(403,"Недостаточно прав")
        return user
    return dep
def audit(user, action, entity, entity_id=None):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO audit_log(user_id,action,entity,entity_id,created_at) VALUES(:uid,:action,:entity,:eid,:created)"), {"uid":user["id"],"action":action,"entity":entity,"eid":entity_id,"created":now()})
def fetch_one(conn, query, params=None): return conn.execute(text(query), params or {}).mappings().first()
def fetch_all(conn, query, params=None): return [dict(r) for r in conn.execute(text(query), params or {}).mappings().all()]
def ensure_exists(conn, table, ident):
    allowed={"clients","applications","trainers","subscriptions","trainings","payments"}
    if table not in allowed: raise HTTPException(500,"Invalid table")
    if not fetch_one(conn, f"SELECT id FROM {table} WHERE id=:id", {"id":ident}): raise HTTPException(404,"Запись не найдена")

class PublicApplication(BaseModel):
    model_config=ConfigDict(str_strip_whitespace=True,extra="ignore")
    name:str=Field(min_length=2,max_length=80); phone:str=Field(min_length=6,max_length=30); direction:str=Field(min_length=2,max_length=80)
    age:Optional[str]=Field("",max_length=10); preferred_time:Optional[str]=Field("",max_length=80); experience:Optional[str]=Field("",max_length=80); comment:Optional[str]=Field("",max_length=1000); website:Optional[str]=Field("",max_length=200)

class ClientIn(BaseModel):
    name:str=Field(min_length=2,max_length=100); phone:str=Field(min_length=6,max_length=30); email:str=""; birth_date:str=""; age:str=""; note:str=""; status:str="active"

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
    name:str=Field(min_length=2,max_length=100); phone:str=""; direction:str=""; note:str=""

class SubscriptionIn(BaseModel):
    client_id:int; type_id:Optional[int]=None; name:str=Field(min_length=2,max_length=120); price:float=Field(default=0,ge=0); lessons_total:Optional[int]=Field(default=None,ge=0); lessons_left:Optional[int]=Field(default=None,ge=0); starts_at:str; ends_at:str; note:str=""

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
    client_id:int; subscription_id:Optional[int]=None; amount:float=Field(gt=0); payment_date:str; method:str="cash"; note:str=""

class PaymentPatch(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0)
    payment_date: Optional[str] = None
    method: Optional[str] = None
    note: Optional[str] = None

class TrainingIn(BaseModel):
    title:str=Field(min_length=2,max_length=120); direction:str=""; trainer_id:Optional[int]=None; training_date:str; start_time:str; end_time:str; room:str=""; capacity:int=Field(default=0,ge=0); note:str=""

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
    training_id:int; client_id:int; status:str="present"; note:str=""

@app.get("/", response_class=HTMLResponse)
async def home():
    f=BASE/"index.html"
    if not f.exists(): raise HTTPException(404,"index.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.get("/health")
async def health():
    try:
        with engine.connect() as conn: conn.execute(text("SELECT 1"))
        return {"ok":True,"service":"bratica","database":"postgresql"}
    except Exception as exc: raise HTTPException(503,"Database unavailable") from exc

@app.get("/admin/login",response_class=HTMLResponse)
async def login_page():
    f=BASE/"admin_login.html"
    if not f.exists(): raise HTTPException(404,"admin_login.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8").replace("{{ERROR}}", ""))

@app.post("/admin/login")
async def login(email:str=Form(...),password:str=Form(...),remember:Optional[str]=Form(None)):
    with engine.connect() as conn: user=fetch_one(conn,"SELECT * FROM users WHERE email=:email AND active=TRUE",{"email":email.strip().lower()})
    if not user or not password_ok(password,user["password_hash"]):
        html=(BASE/"admin_login.html").read_text(encoding="utf-8").replace("{{ERROR}}",'<div class="error">Неверный email или пароль</div>')
        return HTMLResponse(html,status_code=401)
    seconds=REMEMBER_SESSION_SECONDS if remember=="1" else SHORT_SESSION_SECONDS; token=secrets.token_urlsafe(48)
    with engine.begin() as conn: conn.execute(text("INSERT INTO sessions(token,user_id,expires_at) VALUES(:token,:uid,:exp)"),{"token":token,"uid":user["id"],"exp":now()+seconds})
    response=RedirectResponse("/admin",status_code=303)
    response.set_cookie("bratica_session",token,httponly=True,secure=COOKIE_SECURE,samesite="lax",max_age=seconds if remember=="1" else None)
    return response

@app.post("/admin/logout")
async def logout(request:Request):
    token=request.cookies.get("bratica_session")
    if token:
        with engine.begin() as conn: conn.execute(text("DELETE FROM sessions WHERE token=:token"),{"token":token})
    response=RedirectResponse("/admin/login",status_code=303); response.delete_cookie("bratica_session"); return response

@app.get("/admin",response_class=HTMLResponse)
async def admin_page(request:Request):
    try: get_user(request)
    except HTTPException: return RedirectResponse("/admin/login",status_code=303)
    f=BASE/"admin.html"
    if not f.exists(): raise HTTPException(404,"admin.html не найден")
    return HTMLResponse(f.read_text(encoding="utf-8"))

@app.post("/api/applications",status_code=201)
async def create_application(data:PublicApplication):
    if data.website: return {"ok":True,"id":0}
    if sum(ch.isdigit() for ch in data.phone)<10: raise HTTPException(422,"Некорректный телефон")
    with engine.begin() as conn:
        row=conn.execute(text("""INSERT INTO applications(name,phone,direction,age,preferred_time,experience,comment,created_at,updated_at) VALUES(:name,:phone,:direction,:age,:time,:experience,:comment,:created,:updated) RETURNING id"""),{"name":data.name,"phone":data.phone,"direction":data.direction,"age":data.age or "","time":data.preferred_time or "","experience":data.experience or "","comment":data.comment or "","created":now(),"updated":now()}).first()
    return {"ok":True,"id":row.id,"status":"new"}

@app.get("/api/admin/me")
async def me(user=Depends(get_user)): return {k:user[k] for k in ("id","email","name","role")}

@app.get("/api/admin/dashboard")
async def dashboard(user=Depends(get_user)):
    with engine.connect() as c:
        return {"applications_new":c.execute(text("SELECT count(*) FROM applications WHERE status='new'")).scalar(),"clients_active":c.execute(text("SELECT count(*) FROM clients WHERE status='active'")).scalar(),"subscriptions_active":c.execute(text("SELECT count(*) FROM subscriptions WHERE status='active' AND ends_at>=CURRENT_DATE")).scalar(),"visits_today":c.execute(text("SELECT count(*) FROM attendance a JOIN trainings t ON t.id=a.training_id WHERE a.status='present' AND t.training_date=CURRENT_DATE")).scalar(),"expiring":c.execute(text("SELECT count(*) FROM subscriptions WHERE status='active' AND ends_at BETWEEN CURRENT_DATE AND CURRENT_DATE+7")).scalar()}

@app.get("/api/admin/applications")
async def applications(status:str="all",q:str="",user=Depends(get_user)):
    if status!="all" and status not in STATUSES: raise HTTPException(400,"Недопустимый статус")
    conditions=[]; p={}
    if status!="all": conditions.append("status=:status"); p["status"]=status
    if q.strip(): conditions.append("(name ILIKE :q OR phone ILIKE :q OR direction ILIKE :q)"); p["q"]="%"+q.strip()+"%"
    where=" WHERE "+" AND ".join(conditions) if conditions else ""
    with engine.connect() as c: return fetch_all(c,"SELECT * FROM applications"+where+" ORDER BY created_at DESC",p)

@app.patch("/api/admin/applications/{ident}")
async def application_patch(ident: int, data: ApplicationPatch, user=Depends(get_user)):
    if data.status is not None and data.status not in STATUSES:
        raise HTTPException(status_code=422, detail="Недопустимый статус")
    fields = {"name": data.name, "phone": data.phone, "direction": data.direction, "age": data.age, "preferred_time": data.preferred_time, "experience": data.experience, "comment": data.comment, "status": data.status, "admin_note": data.admin_note}
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

@app.post("/api/admin/applications/{ident}/convert")
async def convert_application(ident:int,user=Depends(get_user)):
    with engine.begin() as c:
        a=fetch_one(c,"SELECT * FROM applications WHERE id=:id",{"id":ident})
        if not a: raise HTTPException(404,"Заявка не найдена")
        existing=fetch_one(c,"SELECT id FROM clients WHERE source_application_id=:id",{"id":ident})
        if existing: return {"ok":True,"client_id":existing["id"]}
        row=c.execute(text("INSERT INTO clients(name,phone,age,note,status,source_application_id,created_at,updated_at) VALUES(:name,:phone,:age,:note,'active',:source,:created,:updated) RETURNING id"),{"name":a["name"],"phone":a["phone"],"age":a["age"],"note":a["comment"],"source":ident,"created":now(),"updated":now()}).first()
        c.execute(text("UPDATE applications SET status='completed',updated_at=:t WHERE id=:id"),{"t":now(),"id":ident})
    audit(user,"convert","application",ident); return {"ok":True,"client_id":row.id}

@app.get("/api/admin/clients")
async def clients(q:str="",user=Depends(get_user)):
    sql="""SELECT c.*,(SELECT name FROM subscriptions s WHERE s.client_id=c.id AND s.status='active' ORDER BY s.id DESC LIMIT 1) subscription_name,(SELECT lessons_left FROM subscriptions s WHERE s.client_id=c.id AND s.status='active' ORDER BY s.id DESC LIMIT 1) lessons_left FROM clients c"""
    params={}
    if q.strip(): sql+=" WHERE c.name ILIKE :q OR c.phone ILIKE :q"; params["q"]="%"+q.strip()+"%"
    sql+=" ORDER BY c.created_at DESC"
    with engine.connect() as c: return fetch_all(c,sql,params)

@app.get("/api/admin/clients/{ident}")
async def client_detail(ident:int,user=Depends(get_user)):
    with engine.connect() as c:
        client=fetch_one(c,"SELECT * FROM clients WHERE id=:id",{"id":ident})
        if not client: raise HTTPException(404,"Клиент не найден")
        return {"client":dict(client),"subscriptions":fetch_all(c,"SELECT * FROM subscriptions WHERE client_id=:id ORDER BY id DESC",{"id":ident}),"payments":fetch_all(c,"SELECT * FROM payments WHERE client_id=:id ORDER BY id DESC",{"id":ident}),"attendance":fetch_all(c,"SELECT a.*,t.title,t.training_date,t.start_time FROM attendance a JOIN trainings t ON t.id=a.training_id WHERE a.client_id=:id ORDER BY t.training_date DESC,t.start_time DESC",{"id":ident})}

@app.post("/api/admin/clients")
async def client_create(data:ClientIn,user=Depends(get_user)):
    if data.status not in {"active","frozen","archived"}: raise HTTPException(422,"Недопустимый статус клиента")
    try:
        with engine.begin() as c:
            row=c.execute(text("INSERT INTO clients(name,phone,email,birth_date,age,note,status,created_at,updated_at) VALUES(:name,:phone,:email,:birth,:age,:note,:status,:created,:updated) RETURNING id"),{"name":data.name,"phone":data.phone,"email":data.email,"birth":data.birth_date,"age":data.age,"note":data.note,"status":data.status,"created":now(),"updated":now()}).first()
    except IntegrityError: raise HTTPException(409,"Не удалось создать клиента")
    audit(user,"create","client",row.id); return {"ok":True,"id":row.id}

@app.patch("/api/admin/clients/{ident}")
async def client_update(ident:int,data:ClientIn,user=Depends(get_user)):
    if data.status not in {"active","frozen","archived"}: raise HTTPException(422,"Недопустимый статус клиента")
    with engine.begin() as c:
        result=c.execute(text("UPDATE clients SET name=:name,phone=:phone,email=:email,birth_date=:birth,age=:age,note=:note,status=:status,updated_at=:updated WHERE id=:id"),{"name":data.name,"phone":data.phone,"email":data.email,"birth":data.birth_date,"age":data.age,"note":data.note,"status":data.status,"updated":now(),"id":ident})
        if not result.rowcount: raise HTTPException(404,"Клиент не найден")
    audit(user,"update","client",ident); return {"ok":True}

@app.get("/api/admin/directions")
async def directions(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c,"SELECT * FROM directions WHERE active=TRUE ORDER BY id")

@app.get("/api/public/directions")
async def public_directions():
    with engine.connect() as c: return fetch_all(c,"SELECT id,name FROM directions WHERE active=TRUE ORDER BY id")

@app.get("/api/admin/trainers")
async def trainers(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c,"SELECT * FROM trainers WHERE active=TRUE ORDER BY name")

@app.post("/api/admin/trainers")
async def trainer_create(data:TrainerIn,user=Depends(roles("owner","admin"))):
    with engine.begin() as c: row=c.execute(text("INSERT INTO trainers(name,phone,direction,note) VALUES(:name,:phone,:direction,:note) RETURNING id"),data.model_dump()).first()
    audit(user,"create","trainer",row.id); return {"ok":True,"id":row.id}

@app.patch("/api/admin/trainers/{ident}")
async def trainer_update(ident: int, data: TrainerIn, user=Depends(roles("owner","admin"))):
    with engine.begin() as c:
        res = c.execute(text("UPDATE trainers SET name=:name, phone=:phone, direction=:direction, note=:note WHERE id=:id"), {"id": ident, **data.model_dump()})
        if res.rowcount == 0: raise HTTPException(404, "Тренер не найден")
    audit(user, "update", "trainer", ident)
    return {"ok": True}

@app.get("/api/admin/subscriptions")
async def subscriptions(user=Depends(get_user)):
    with engine.connect() as c: return fetch_all(c,"SELECT s.*,c.name client_name,c.phone FROM subscriptions s JOIN clients c ON c.id=s.client_id ORDER BY s.id DESC")

@app.post("/api/admin/subscriptions")
async def subscription_create(data:SubscriptionIn,user=Depends(get_user)):
    if data.ends_at<data.starts_at: raise HTTPException(422,"Дата окончания раньше даты начала")
    with engine.begin() as c:
        ensure_exists(c,"clients",data.client_id)
        left=data.lessons_left if data.lessons_left is not None else data.lessons_total
        p=data.model_dump(); p["lessons_left"]=left; p["created_at"]=now()
        row=c.execute(text("INSERT INTO subscriptions(client_id,type_id,name,price,lessons_total,lessons_left,starts_at,ends_at,note,created_at) VALUES(:client_id,:type_id,:name,:price,:lessons_total,:lessons_left,:starts_at,:ends_at,:note,:created_at) RETURNING id"),p).first()
    audit(user,"create","subscription",row.id); return {"ok":True,"id":row.id}

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
    with engine.connect() as c: return fetch_all(c,"SELECT p.*,c.name client_name FROM payments p JOIN clients c ON c.id=p.client_id ORDER BY p.id DESC")

@app.post("/api/admin/payments")
async def payment_create(data:PaymentIn,user=Depends(get_user)):
    with engine.begin() as c:
        ensure_exists(c,"clients",data.client_id)
        if data.subscription_id:
            sub=fetch_one(c,"SELECT * FROM subscriptions WHERE id=:id",{"id":data.subscription_id})
            if not sub or sub["client_id"]!=data.client_id: raise HTTPException(422,"Абонемент не принадлежит клиенту")
        p=data.model_dump(); p["created_at"]=now()
        row=c.execute(text("INSERT INTO payments(client_id,subscription_id,amount,payment_date,method,note,created_at) VALUES(:client_id,:subscription_id,:amount,:payment_date,:method,:note,:created_at) RETURNING id"),p).first()
    audit(user,"create","payment",row.id); return {"ok":True,"id":row.id}

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
    with engine.connect() as c: return fetch_all(c,"SELECT t.*,tr.name trainer_name FROM trainings t LEFT JOIN trainers tr ON tr.id=t.trainer_id WHERE t.active=TRUE ORDER BY training_date DESC,start_time")

@app.post("/api/admin/trainings")
async def training_create(data:TrainingIn,user=Depends(get_user)):
    if data.end_time<=data.start_time: raise HTTPException(422,"Окончание должно быть позже начала")
    with engine.begin() as c:
        if data.trainer_id: ensure_exists(c,"trainers",data.trainer_id)
        p=data.model_dump()
        row=c.execute(text("INSERT INTO trainings(title,direction,trainer_id,training_date,start_time,end_time,room,capacity,note) VALUES(:title,:direction,:trainer_id,:training_date,:start_time,:end_time,:room,:capacity,:note) RETURNING id"),p).first()
    audit(user,"create","training",row.id); return {"ok":True,"id":row.id}

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

@app.get("/api/admin/trainings/{ident}/attendance")
async def training_attendance(ident:int,user=Depends(get_user)):
    with engine.connect() as c:
        ensure_exists(c,"trainings",ident)
        return fetch_all(c,"SELECT a.*,c.name,c.phone FROM attendance a JOIN clients c ON c.id=a.client_id WHERE a.training_id=:id ORDER BY c.name",{"id":ident})

@app.post("/api/admin/attendance")
async def attendance(data:AttendanceIn,user=Depends(get_user)):
    if data.status not in ATTENDANCE_STATUSES: raise HTTPException(422,"Недопустимый статус посещения")
    with engine.begin() as c:
        ensure_exists(c,"trainings",data.training_id); ensure_exists(c,"clients",data.client_id)
        old=fetch_one(c,"SELECT status FROM attendance WHERE training_id=:training AND client_id=:client FOR UPDATE",{"training":data.training_id,"client":data.client_id})
        c.execute(text("INSERT INTO attendance(training_id,client_id,status,note,created_at) VALUES(:training,:client,:status,:note,:created) ON CONFLICT(training_id,client_id) DO UPDATE SET status=EXCLUDED.status,note=EXCLUDED.note,created_at=EXCLUDED.created_at"),{"training":data.training_id,"client":data.client_id,"status":data.status,"note":data.note,"created":now()})
        delta=1 if old and old["status"]=="present" and data.status!="present" else (-1 if (not old or old["status"]!="present") and data.status=="present" else 0)
        if delta:
            sub=fetch_one(c,"SELECT id,lessons_left FROM subscriptions WHERE client_id=:client AND status='active' AND starts_at<=CURRENT_DATE AND ends_at>=CURRENT_DATE AND (lessons_left IS NULL OR lessons_left>0) ORDER BY id DESC LIMIT 1 FOR UPDATE",{"client":data.client_id})
            if sub and sub["lessons_left"] is not None: c.execute(text("UPDATE subscriptions SET lessons_left=GREATEST(0,lessons_left+:delta) WHERE id=:id"),{"delta":delta,"id":sub["id"]})
    audit(user,"upsert","attendance",data.training_id); return {"ok":True}
