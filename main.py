import os
import sqlite3
import time
import secrets
import hashlib
import hmac
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict


# ============================================================
# НАСТРОЙКИ
# ============================================================

BASE = Path(__file__).resolve().parent
STATIC_DIR = BASE / "static"
STATIC_DIR.mkdir(exist_ok=True)

DB = os.getenv("DB_FILE", str(BASE / "bratica.db"))
SESSION_DAYS = 30
SHORT_SESSION_SECONDS = 8 * 60 * 60
REMEMBER_SESSION_SECONDS = SESSION_DAYS * 24 * 60 * 60

# Для локального запуска COOKIE_SECURE=false.
# Для Render с HTTPS можно поставить COOKIE_SECURE=true.
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"

STATUSES = {"new", "contacted", "booked", "completed", "cancelled"}
ROLES = {"owner", "admin", "trainer"}
ATTENDANCE_STATUSES = {"present", "absent", "cancelled", "trial"}


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def now() -> int:
    return int(time.time())


def db():
    """Открывает соединение с SQLite."""
    db_path = Path(DB)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(db_path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_db():
    """Создаёт таблицы CRM, если их ещё нет."""
    connection = db()

    with connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'admin',
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS applications (
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
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS directions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trainers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT DEFAULT '',
                direction TEXT DEFAULT '',
                note TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                email TEXT DEFAULT '',
                birth_date TEXT DEFAULT '',
                age TEXT DEFAULT '',
                note TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                source_application_id INTEGER,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscription_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                direction TEXT DEFAULT '',
                price REAL NOT NULL DEFAULT 0,
                lessons INTEGER,
                duration_days INTEGER,
                freeze_days INTEGER DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                type_id INTEGER,
                name TEXT NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                lessons_total INTEGER,
                lessons_left INTEGER,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                frozen_until TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY(type_id) REFERENCES subscription_types(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS trainings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                direction TEXT DEFAULT '',
                trainer_id INTEGER,
                training_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                room TEXT DEFAULT '',
                capacity INTEGER DEFAULT 0,
                note TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(trainer_id) REFERENCES trainers(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS attendance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                training_id INTEGER NOT NULL,
                client_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'present',
                note TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                UNIQUE(training_id, client_id),
                FOREIGN KEY(training_id) REFERENCES trainings(id) ON DELETE CASCADE,
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL,
                subscription_id INTEGER,
                amount REAL NOT NULL,
                payment_date TEXT NOT NULL,
                method TEXT NOT NULL DEFAULT 'cash',
                note TEXT DEFAULT '',
                created_at INTEGER NOT NULL,
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
                FOREIGN KEY(subscription_id) REFERENCES subscriptions(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT NOT NULL,
                entity TEXT NOT NULL,
                entity_id INTEGER,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
            );
            """
        )

        default_directions = [
            "Тайский бокс",
            "Бокс",
            "Киокусинкай-каратэ",
            "Йога",
            "BRATICA Transform",
        ]

        for direction in default_directions:
            connection.execute(
                "INSERT OR IGNORE INTO directions(name) VALUES(?)",
                (direction,),
            )

    connection.close()


# ============================================================
# ПАРОЛИ И ПЕРВЫЙ ПОЛЬЗОВАТЕЛЬ
# ============================================================

def password_hash(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        210_000,
    )
    return f"{salt.hex()}:{digest.hex()}"


def password_ok(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        calculated = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            210_000,
        ).hex()
        return hmac.compare_digest(calculated, digest_hex)
    except Exception:
        return False


def bootstrap_user():
    """Создаёт владельца при первом запуске из переменных Render."""
    email = os.getenv("ADMIN_EMAIL", "").strip().lower()
    password = os.getenv("ADMIN_PASSWORD", "")

    if not email or not password:
        return

    connection = db()
    with connection:
        existing = connection.execute(
            "SELECT id FROM users WHERE email=?",
            (email,),
        ).fetchone()

        if not existing:
            connection.execute(
                """
                INSERT INTO users(email, name, password_hash, role, created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    email,
                    "Владелец",
                    password_hash(password),
                    "owner",
                    now(),
                ),
            )

    connection.close()


# ============================================================
# FASTAPI
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_user()
    yield


app = FastAPI(title="BRATICA CRM", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# АВТОРИЗАЦИЯ И СЛУЖЕБНЫЕ ФУНКЦИИ
# ============================================================

def get_user(request: Request):
    token = request.cookies.get("bratica_session")

    if not token:
        raise HTTPException(status_code=401, detail="Требуется авторизация")

    connection = db()
    row = connection.execute(
        """
        SELECT u.*
        FROM sessions s
        JOIN users u ON u.id=s.user_id
        WHERE s.token=?
          AND s.expires_at>?
          AND u.active=1
        """,
        (token, now()),
    ).fetchone()
    connection.close()

    if not row:
        raise HTTPException(status_code=401, detail="Сессия истекла")

    return dict(row)


def roles(*allowed_roles):
    def dependency(user=Depends(get_user)):
        if user["role"] not in allowed_roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user

    return dependency


def audit(user, action: str, entity: str, entity_id: Optional[int] = None):
    connection = db()
    with connection:
        connection.execute(
            """
            INSERT INTO audit_log(user_id, action, entity, entity_id, created_at)
            VALUES(?,?,?,?,?)
            """,
            (user["id"], action, entity, entity_id, now()),
        )
    connection.close()


def row_or_404(connection, table: str, ident: int):
    allowed_tables = {
        "applications",
        "clients",
        "trainers",
        "subscriptions",
        "trainings",
        "payments",
    }

    if table not in allowed_tables:
        raise HTTPException(status_code=500, detail="Недопустимая таблица")

    row = connection.execute(
        f"SELECT * FROM {table} WHERE id=?",
        (ident,),
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    return row


# ============================================================
# Pydantic-модели
# ============================================================

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


class PaymentIn(BaseModel):
    client_id: int
    subscription_id: Optional[int] = None
    amount: float = Field(gt=0)
    payment_date: str
    method: str = "cash"
    note: str = ""


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


class AttendanceIn(BaseModel):
    training_id: int
    client_id: int
    status: str = "present"
    note: str = ""


# ============================================================
# ПУБЛИЧНЫЙ САЙТ
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def home():
    file = BASE / "index.html"

    if not file.exists():
        raise HTTPException(status_code=404, detail="index.html не найден")

    return HTMLResponse(file.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"ok": True, "service": "bratica-fight-club"}


@app.post("/api/applications", status_code=201)
async def create_application(data: PublicApplication):
    # Honeypot для защиты формы от простых ботов
    if data.website:
        return {"ok": True, "id": 0}

    if sum(char.isdigit() for char in data.phone) < 10:
        raise HTTPException(status_code=422, detail="Некорректный телефон")

    connection = db()
    timestamp = now()

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO applications(
                name, phone, direction, age, preferred_time,
                experience, comment, created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                data.name,
                data.phone,
                data.direction,
                data.age or "",
                data.preferred_time or "",
                data.experience or "",
                data.comment or "",
                timestamp,
                timestamp,
            ),
        )
        application_id = cursor.lastrowid

    connection.close()

    return {"ok": True, "id": application_id, "status": "new"}


# ============================================================
# ВХОД И АДМИН-ПАНЕЛЬ
# ============================================================

@app.get("/admin/login", response_class=HTMLResponse)
async def login_page():
    file = BASE / "admin_login.html"

    if not file.exists():
        raise HTTPException(status_code=404, detail="admin_login.html не найден")

    return HTMLResponse(file.read_text(encoding="utf-8"))


@app.post("/admin/login")
async def login(
    email: str = Form(...),
    password: str = Form(...),
    remember: Optional[str] = Form(None),
):

    connection = db()
    user = connection.execute(
        "SELECT * FROM users WHERE email=? AND active=1",
        (email.strip().lower(),),
    ).fetchone()

    if not user or not password_ok(password, user["password_hash"]):
        html = (BASE / "admin_login.html").read_text(encoding="utf-8")
        error_html = '<div class="error">Неверный email или пароль</div>'
        html = html.replace("{{ERROR}}", error_html)
        html = html.replace('<div id="error"></div>', error_html)
        connection.close()
        return HTMLResponse(html, status_code=401)

    token = secrets.token_urlsafe(48)

    with connection:
        connection.execute(
            "INSERT INTO sessions(token, user_id, expires_at) VALUES(?,?,?)",
            session_seconds = (
    REMEMBER_SESSION_SECONDS
    if remember == "1"
    else SHORT_SESSION_SECONDS
)

(token, user["id"], now() + session_seconds)


    connection.close()

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        "bratica_session",
        token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=session_seconds if remember == "1" else None,

    )
    return response


@app.post("/admin/logout")
async def logout(request: Request):
    token = request.cookies.get("bratica_session")
    connection = db()

    if token:
        with connection:
            connection.execute(
                "DELETE FROM sessions WHERE token=?",
                (token,),
            )

    connection.close()

    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie("bratica_session")
    return response


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    try:
        get_user(request)
    except HTTPException:
        return RedirectResponse("/admin/login", status_code=303)

    file = BASE / "admin.html"

    if not file.exists():
        raise HTTPException(status_code=404, detail="admin.html не найден")

    return HTMLResponse(file.read_text(encoding="utf-8"))


# ============================================================
# СЛУЖЕБНЫЕ API CRM
# ============================================================

@app.get("/api/admin/me")
async def me(user=Depends(get_user)):
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
    }


@app.get("/api/admin/dashboard")
async def dashboard(user=Depends(get_user)):
    connection = db()

    result = {
        "applications_new": connection.execute(
            "SELECT COUNT(*) FROM applications WHERE status='new'"
        ).fetchone()[0],
        "clients_active": connection.execute(
            "SELECT COUNT(*) FROM clients WHERE status='active'"
        ).fetchone()[0],
        "subscriptions_active": connection.execute(
            """
            SELECT COUNT(*) FROM subscriptions
            WHERE status='active' AND ends_at>=date('now')
            """
        ).fetchone()[0],
        "visits_today": connection.execute(
            """
            SELECT COUNT(*)
            FROM attendance a
            JOIN trainings t ON t.id=a.training_id
            WHERE a.status='present'
              AND t.training_date=date('now','localtime')
            """
        ).fetchone()[0],
        "expiring": connection.execute(
            """
            SELECT COUNT(*) FROM subscriptions
            WHERE status='active'
              AND ends_at BETWEEN date('now') AND date('now','+7 day')
            """
        ).fetchone()[0],
    }

    connection.close()
    return result


# ============================================================
# ЗАЯВКИ
# ============================================================

@app.get("/api/admin/applications")
async def applications(status: str = "all", q: str = "", user=Depends(get_user)):
    connection = db()
    conditions = []
    arguments = []

    if status != "all":
        if status not in STATUSES:
            connection.close()
            raise HTTPException(status_code=400, detail="Недопустимый статус")
        conditions.append("status=?")
        arguments.append(status)

    if q.strip():
        conditions.append("(name LIKE ? OR phone LIKE ? OR direction LIKE ?)")
        arguments.extend([f"%{q.strip()}%"] * 3)

    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    rows = connection.execute(
        "SELECT * FROM applications" + where + " ORDER BY created_at DESC",
        arguments,
    ).fetchall()

    connection.close()
    return [dict(row) for row in rows]


@app.patch("/api/admin/applications/{ident}")
async def application_patch(
    ident: int,
    data: ApplicationPatch,
    user=Depends(get_user),
):
    if data.status is not None and data.status not in STATUSES:
        raise HTTPException(status_code=422, detail="Недопустимый статус")

    sets = []
    arguments = []

    if data.status is not None:
        sets.append("status=?")
        arguments.append(data.status)

    if data.admin_note is not None:
        sets.append("admin_note=?")
        arguments.append(data.admin_note)

    if not sets:
        raise HTTPException(status_code=400, detail="Нет изменений")

    sets.append("updated_at=?")
    arguments.extend([now(), ident])

    connection = db()
    with connection:
        cursor = connection.execute(
            "UPDATE applications SET " + ",".join(sets) + " WHERE id=?",
            arguments,
        )

    connection.close()

    if not cursor.rowcount:
        raise HTTPException(status_code=404, detail="Заявка не найдена")

    audit(user, "update", "application", ident)
    return {"ok": True}


@app.post("/api/admin/applications/{ident}/convert")
async def convert_application(ident: int, user=Depends(get_user)):
    connection = db()
    application = row_or_404(connection, "applications", ident)

    existing = connection.execute(
        "SELECT id FROM clients WHERE source_application_id=?",
        (ident,),
    ).fetchone()

    if existing:
        connection.close()
        return {"ok": True, "client_id": existing["id"]}

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO clients(
                name, phone, age, note, status,
                source_application_id, created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                application["name"],
                application["phone"],
                application["age"],
                application["comment"],
                "active",
                ident,
                now(),
                now(),
            ),
        )
        client_id = cursor.lastrowid

        connection.execute(
            "UPDATE applications SET status='completed', updated_at=? WHERE id=?",
            (now(), ident),
        )

    connection.close()
    audit(user, "convert", "application", ident)
    return {"ok": True, "client_id": client_id}


# ============================================================
# КЛИЕНТЫ
# ============================================================

@app.get("/api/admin/clients")
async def clients(q: str = "", user=Depends(get_user)):
    connection = db()
    condition = ""
    arguments = []

    if q.strip():
        condition = "WHERE c.name LIKE ? OR c.phone LIKE ?"
        arguments = [f"%{q.strip()}%"] * 2

    rows = connection.execute(
        """
        SELECT
            c.*,
            (
                SELECT name FROM subscriptions s
                WHERE s.client_id=c.id AND s.status='active'
                ORDER BY s.id DESC LIMIT 1
            ) AS subscription_name,
            (
                SELECT lessons_left FROM subscriptions s
                WHERE s.client_id=c.id AND s.status='active'
                ORDER BY s.id DESC LIMIT 1
            ) AS lessons_left
        FROM clients c
        """ + condition + " ORDER BY c.created_at DESC",
        arguments,
    ).fetchall()

    connection.close()
    return [dict(row) for row in rows]


@app.get("/api/admin/clients/{ident}")
async def client_detail(ident: int, user=Depends(get_user)):
    connection = db()
    client = row_or_404(connection, "clients", ident)

    subscriptions = connection.execute(
        "SELECT * FROM subscriptions WHERE client_id=? ORDER BY id DESC",
        (ident,),
    ).fetchall()

    payments = connection.execute(
        "SELECT * FROM payments WHERE client_id=? ORDER BY id DESC",
        (ident,),
    ).fetchall()

    attendance = connection.execute(
        """
        SELECT a.*, t.title, t.training_date, t.start_time
        FROM attendance a
        JOIN trainings t ON t.id=a.training_id
        WHERE a.client_id=?
        ORDER BY t.training_date DESC, t.start_time DESC
        """,
        (ident,),
    ).fetchall()

    connection.close()

    return {
        "client": dict(client),
        "subscriptions": [dict(row) for row in subscriptions],
        "payments": [dict(row) for row in payments],
        "attendance": [dict(row) for row in attendance],
    }


@app.post("/api/admin/clients")
async def client_create(data: ClientIn, user=Depends(get_user)):
    if data.status not in {"active", "frozen", "archived"}:
        raise HTTPException(status_code=422, detail="Недопустимый статус клиента")

    connection = db()
    duplicate = connection.execute(
        "SELECT id FROM clients WHERE phone=? AND status!='archived'",
        (data.phone,),
    ).fetchone()

    if duplicate:
        connection.close()
        raise HTTPException(
            status_code=409,
            detail="Клиент с таким телефоном уже существует",
        )

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO clients(
                name, phone, email, birth_date, age,
                note, status, created_at, updated_at
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                data.name,
                data.phone,
                data.email,
                data.birth_date,
                data.age,
                data.note,
                data.status,
                now(),
                now(),
            ),
        )
        ident = cursor.lastrowid

    connection.close()
    audit(user, "create", "client", ident)
    return {"ok": True, "id": ident}


@app.patch("/api/admin/clients/{ident}")
async def client_update(
    ident: int,
    data: ClientIn,
    user=Depends(get_user),
):
    if data.status not in {"active", "frozen", "archived"}:
        raise HTTPException(status_code=422, detail="Недопустимый статус клиента")

    connection = db()
    row_or_404(connection, "clients", ident)

    with connection:
        connection.execute(
            """
            UPDATE clients
            SET name=?, phone=?, email=?, birth_date=?, age=?,
                note=?, status=?, updated_at=?
            WHERE id=?
            """,
            (
                data.name,
                data.phone,
                data.email,
                data.birth_date,
                data.age,
                data.note,
                data.status,
                now(),
                ident,
            ),
        )

    connection.close()
    audit(user, "update", "client", ident)
    return {"ok": True}


# ============================================================
# НАПРАВЛЕНИЯ И ТРЕНЕРЫ
# ============================================================

@app.get("/api/admin/directions")
async def directions(user=Depends(get_user)):
    connection = db()
    rows = connection.execute(
        "SELECT * FROM directions WHERE active=1 ORDER BY id"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


@app.get("/api/admin/trainers")
async def trainers(user=Depends(get_user)):
    connection = db()
    rows = connection.execute(
        "SELECT * FROM trainers WHERE active=1 ORDER BY name"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


@app.post("/api/admin/trainers")
async def trainer_create(
    data: TrainerIn,
    user=Depends(roles("owner", "admin")),
):
    connection = db()

    with connection:
        cursor = connection.execute(
            "INSERT INTO trainers(name, phone, direction, note) VALUES(?,?,?,?)",
            (data.name, data.phone, data.direction, data.note),
        )
        ident = cursor.lastrowid

    connection.close()
    audit(user, "create", "trainer", ident)
    return {"ok": True, "id": ident}


# ============================================================
# АБОНЕМЕНТЫ
# ============================================================

@app.get("/api/admin/subscriptions")
async def subscriptions(user=Depends(get_user)):
    connection = db()
    rows = connection.execute(
        """
        SELECT s.*, c.name AS client_name, c.phone
        FROM subscriptions s
        JOIN clients c ON c.id=s.client_id
        ORDER BY s.id DESC
        """
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


@app.post("/api/admin/subscriptions")
async def subscription_create(
    data: SubscriptionIn,
    user=Depends(get_user),
):
    if data.ends_at < data.starts_at:
        raise HTTPException(
            status_code=422,
            detail="Дата окончания раньше даты начала",
        )

    connection = db()
    row_or_404(connection, "clients", data.client_id)

    lessons_left = (
        data.lessons_left
        if data.lessons_left is not None
        else data.lessons_total
    )

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO subscriptions(
                client_id, type_id, name, price,
                lessons_total, lessons_left,
                starts_at, ends_at, note, created_at
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                data.client_id,
                data.type_id,
                data.name,
                data.price,
                data.lessons_total,
                lessons_left,
                data.starts_at,
                data.ends_at,
                data.note,
                now(),
            ),
        )
        ident = cursor.lastrowid

    connection.close()
    audit(user, "create", "subscription", ident)
    return {"ok": True, "id": ident}


# ============================================================
# ОПЛАТЫ
# ============================================================

@app.get("/api/admin/payments")
async def payments(user=Depends(get_user)):
    connection = db()
    rows = connection.execute(
        """
        SELECT p.*, c.name AS client_name
        FROM payments p
        JOIN clients c ON c.id=p.client_id
        ORDER BY p.id DESC
        """
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


@app.post("/api/admin/payments")
async def payment_create(
    data: PaymentIn,
    user=Depends(get_user),
):
    connection = db()
    row_or_404(connection, "clients", data.client_id)

    if data.subscription_id is not None:
        subscription = row_or_404(
            connection,
            "subscriptions",
            data.subscription_id,
        )
        if subscription["client_id"] != data.client_id:
            connection.close()
            raise HTTPException(
                status_code=422,
                detail="Абонемент не принадлежит выбранному клиенту",
            )

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO payments(
                client_id, subscription_id, amount,
                payment_date, method, note, created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                data.client_id,
                data.subscription_id,
                data.amount,
                data.payment_date,
                data.method,
                data.note,
                now(),
            ),
        )
        ident = cursor.lastrowid

    connection.close()
    audit(user, "create", "payment", ident)
    return {"ok": True, "id": ident}


# ============================================================
# РАСПИСАНИЕ
# ============================================================

@app.get("/api/admin/trainings")
async def trainings(user=Depends(get_user)):
    connection = db()
    rows = connection.execute(
        """
        SELECT t.*, tr.name AS trainer_name
        FROM trainings t
        LEFT JOIN trainers tr ON tr.id=t.trainer_id
        WHERE t.active=1
        ORDER BY training_date DESC, start_time
        """
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


@app.post("/api/admin/trainings")
async def training_create(
    data: TrainingIn,
    user=Depends(get_user),
):
    if data.end_time <= data.start_time:
        raise HTTPException(
            status_code=422,
            detail="Окончание должно быть позже начала",
        )

    connection = db()

    if data.trainer_id is not None:
        row_or_404(connection, "trainers", data.trainer_id)

    with connection:
        cursor = connection.execute(
            """
            INSERT INTO trainings(
                title, direction, trainer_id,
                training_date, start_time, end_time,
                room, capacity, note
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                data.title,
                data.direction,
                data.trainer_id,
                data.training_date,
                data.start_time,
                data.end_time,
                data.room,
                data.capacity,
                data.note,
            ),
        )
        ident = cursor.lastrowid

    connection.close()
    audit(user, "create", "training", ident)
    return {"ok": True, "id": ident}


@app.get("/api/admin/trainings/{ident}/attendance")
async def training_attendance(ident: int, user=Depends(get_user)):
    connection = db()
    row_or_404(connection, "trainings", ident)

    rows = connection.execute(
        """
        SELECT a.*, c.name, c.phone
        FROM attendance a
        JOIN clients c ON c.id=a.client_id
        WHERE a.training_id=?
        ORDER BY c.name
        """,
        (ident,),
    ).fetchall()

    connection.close()
    return [dict(row) for row in rows]


# ============================================================
# ПОСЕЩЕНИЯ
# ============================================================

@app.post("/api/admin/attendance")
async def attendance(
    data: AttendanceIn,
    user=Depends(get_user),
):
    if data.status not in ATTENDANCE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Недопустимый статус посещения",
        )

    connection = db()
    row_or_404(connection, "trainings", data.training_id)
    row_or_404(connection, "clients", data.client_id)

    old = connection.execute(
        """
        SELECT status FROM attendance
        WHERE training_id=? AND client_id=?
        """,
        (data.training_id, data.client_id),
    ).fetchone()

    with connection:
        connection.execute(
            """
            INSERT INTO attendance(
                training_id, client_id, status, note, created_at
            )
            VALUES(?,?,?,?,?)
            ON CONFLICT(training_id, client_id)
            DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                created_at=excluded.created_at
            """,
            (
                data.training_id,
                data.client_id,
                data.status,
                data.note,
                now(),
            ),
        )

        # Списываем занятие только при переходе в present.
        # Возвращаем занятие при переходе из present в другой статус.
        if old and old["status"] == "present" and data.status != "present":
            delta = 1
        elif (not old or old["status"] != "present") and data.status == "present":
            delta = -1
        else:
            delta = 0

        if delta:
            subscription = connection.execute(
                """
                SELECT id, lessons_left
                FROM subscriptions
                WHERE client_id=?
                  AND status='active'
                  AND (lessons_left IS NULL OR lessons_left>0)
                  AND starts_at<=date('now')
                  AND ends_at>=date('now')
                ORDER BY id DESC
                LIMIT 1
                """,
                (data.client_id,),
            ).fetchone()

            if subscription and subscription["lessons_left"] is not None:
                connection.execute(
                    """
                    UPDATE subscriptions
                    SET lessons_left=MAX(0, lessons_left+?)
                    WHERE id=?
                    """,
                    (delta, subscription["id"]),
                )

    connection.close()
    audit(user, "upsert", "attendance", data.training_id)
    return {"ok": True}


# ============================================================
# ПУБЛИЧНЫЕ ДАННЫЕ ДЛЯ САЙТА
# ============================================================

@app.get("/api/public/directions")
async def public_directions():
    connection = db()
    rows = connection.execute(
        "SELECT id, name FROM directions WHERE active=1 ORDER BY id"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


# ============================================================
# ЛОКАЛЬНЫЙ ЗАПУСК
# ============================================================
print("BASE:", BASE)
print("admin_login exists:", (BASE / "admin_login.html").exists())
print("files:", [item.name for item in BASE.iterdir()])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
