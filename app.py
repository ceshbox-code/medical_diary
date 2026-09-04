import os
import sqlite3
import secrets
import json
from datetime import datetime, date, timedelta
from functools import wraps
from io import BytesIO
from xml.sax.saxutils import escape

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    render_template_string,
    send_file,
    g,
    abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle

try:
    from webauthn import (
        generate_registration_options,
        verify_registration_response,
        generate_authentication_options,
        verify_authentication_response,
    )

    try:
        from webauthn import options_to_json
    except Exception:
        from webauthn.helpers import options_to_json

    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes

    try:
        from webauthn.helpers import (
            parse_registration_credential_json,
            parse_authentication_credential_json,
        )
    except Exception:
        from webauthn.helpers.structs import (
            RegistrationCredential,
            AuthenticationCredential,
        )

        def parse_registration_credential_json(s):
            if hasattr(RegistrationCredential, "model_validate_json"):
                return RegistrationCredential.model_validate_json(s)
            return RegistrationCredential.parse_raw(s)

        def parse_authentication_credential_json(s):
            if hasattr(AuthenticationCredential, "model_validate_json"):
                return AuthenticationCredential.model_validate_json(s)
            return AuthenticationCredential.parse_raw(s)

    from webauthn.helpers.structs import (
        PublicKeyCredentialDescriptor,
        AuthenticatorSelectionCriteria,
        UserVerificationRequirement,
    AuthenticatorAttachment,
    ResidentKeyRequirement,
    )

    WA_AVAILABLE = True
    print("WebAuthn available: True", flush=True)
except Exception as _wa_import_error:
    WA_AVAILABLE = False
    print("WebAuthn import error:", repr(_wa_import_error), flush=True)

DATABASE = os.getenv("DATABASE_PATH", "/data/medical_diary.db")

FONT_CANDIDATES = [
    os.getenv("FONT_PATH", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "DejaVuSans.ttf"),
]
FONT_NAME = "Helvetica"

for _font_path in FONT_CANDIDATES:
    if _font_path and os.path.exists(_font_path):
        pdfmetrics.registerFont(TTFont("DejaVuSans", _font_path))
        FONT_NAME = "DejaVuSans"
        break

if FONT_NAME == "Helvetica":
    # Base14 Helvetica не поддерживает кириллицу — PDF на русском языке
    # будет нечитаемым. Это критично для медицинского дневника, поэтому
    # предупреждение выводится явно при старте, а не тонет в логах.
    print(
        "WARNING: DejaVuSans.ttf не найден ни по одному из путей "
        f"{FONT_CANDIDATES}. Экспорт PDF на русском языке будет повреждён. "
        "Задайте переменную окружения FONT_PATH или положите шрифт в ./fonts/DejaVuSans.ttf.",
        flush=True,
    )


app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="medical_diary_session",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
app.permanent_session_lifetime = timedelta(days=int(os.getenv("SESSION_LIFETIME_DAYS", "30")))


UNIT_RU = {"g": "г", "ml": "мл", "pcs": "шт", "portion": "порция"}

REF_RANGES = {
    "glucose_fasting": (3.3, 5.5),
    "glucose_post": (3.3, 7.8),
    "systolic": (90, 120),
    "diastolic": (60, 80),
    "pulse": (60, 100),
}

STATUS_COLORS = {"low": "#fff3c4", "ok": "#d9f2d9", "high": "#fbd9d9"}


def status_of(value, low, high):
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "ok"




def glucose_assessment(value_mmol_l, glucose_type):
    try:
        value = float(value_mmol_l)
    except Exception:
        return "", "", "ok"

    key = "glucose_fasting" if glucose_type == "fasting" else "glucose_post"
    low, high = REF_RANGES.get(key, REF_RANGES["glucose_fasting"])
    st = status_of(value, low, high)

    if st == "low":
        return (
            "Ниже ориентировочного диапазона",
            "Отметьте самочувствие и повторите измерение. При симптомах или повторяющихся низких значениях обратитесь к врачу.",
            st,
        )

    if st == "high":
        return (
            "Выше ориентировочного диапазона",
            "Отметьте самочувствие и повторите измерение. При повторных высоких значениях обратитесь к врачу.",
            st,
        )

    return (
        "В пределах ориентировочного диапазона",
        "Продолжайте наблюдение по вашему плану.",
        st,
    )


def vitals_assessment(systolic, diastolic, pulse):
    try:
        s = int(systolic)
        d = int(diastolic)
    except Exception:
        return "", "", "ok"

    p = None
    if pulse is not None and str(pulse).strip() != "":
        try:
            p = int(pulse)
        except Exception:
            p = None

    s_st = status_of(s, *REF_RANGES["systolic"])
    d_st = status_of(d, *REF_RANGES["diastolic"])
    p_st = status_of(p, *REF_RANGES["pulse"]) if p is not None else None

    if s >= 180 or d >= 120:
        assessment = "Очень высокое давление"
        recommendation = (
            "Если значение подтверждается после отдыха и/или есть тревожные симптомы, "
            "обратитесь за медицинской помощью."
        )
        overall = "high"
    elif s_st == "high" or d_st == "high":
        assessment = "Давление выше ориентировочного диапазона"
        recommendation = (
            "Отдохните спокойно 5 минут и повторите измерение. "
            "При повторных повышениях обратитесь к врачу."
        )
        overall = "high"
    elif s_st == "low" or d_st == "low":
        assessment = "Давление ниже ориентировочного диапазона"
        recommendation = (
            "Отметьте самочувствие. При головокружении, слабости или повторяющихся "
            "низких значениях обратитесь к врачу."
        )
        overall = "low"
    else:
        assessment = "Давление в пределах ориентировочного диапазона"
        recommendation = "Продолжайте регулярные наблюдения."
        overall = "ok"

    if p is not None:
        if p_st == "low":
            assessment += "; пульс ниже диапазона"
            recommendation += " Отметьте самочувствие; при слабости или головокружении обратитесь к врачу."
            if overall == "ok":
                overall = "low"
        elif p_st == "high":
            assessment += "; пульс выше диапазона"
            recommendation += " Повторите измерение в покое; при повторных повышениях обратитесь к врачу."
            if overall == "ok":
                overall = "high"

    return assessment, recommendation, overall


def food_assessment():
    return (
        "Запись о питании",
        "Сопоставляйте время и количество с уровнем глюкозы и рекомендациями вашего врача.",
        "ok",
    )


def add_assessments(entries):
    for e in entries:
        try:
            if e.get("type") == "glucose":
                assessment, recommendation, status = glucose_assessment(
                    e.get("value_mmol_l"),
                    e.get("glucose_type", "fasting"),
                )
            elif e.get("type") == "vitals":
                assessment, recommendation, status = vitals_assessment(
                    e.get("systolic_mmhg"),
                    e.get("diastolic_mmhg"),
                    e.get("pulse_bpm"),
                )
            else:
                assessment, recommendation, status = food_assessment()

            e["assessment"] = assessment
            e["recommendation"] = recommendation
            e["assessment_status"] = status
        except Exception:
            e["assessment"] = ""
            e["recommendation"] = ""
            e["assessment_status"] = "ok"

    return entries

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS glucose_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  glucose_type TEXT NOT NULL CHECK (glucose_type IN ('fasting', 'post_meal')),
  value_mmol_l REAL NOT NULL CHECK (value_mmol_l BETWEEN 0.1 AND 100.0),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_glucose_user_time ON glucose_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS blood_pressure_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  systolic_mmhg INTEGER NOT NULL CHECK (systolic_mmhg BETWEEN 30 AND 400),
  diastolic_mmhg INTEGER NOT NULL CHECK (diastolic_mmhg BETWEEN 10 AND 300),
  pulse_bpm INTEGER CHECK (pulse_bpm IS NULL OR pulse_bpm BETWEEN 20 AND 300),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT,
  CHECK (systolic_mmhg > diastolic_mmhg)
);

CREATE INDEX IF NOT EXISTS idx_bp_user_time ON blood_pressure_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS food_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  food_name TEXT NOT NULL,
  consumed_at TEXT NOT NULL,
  amount_value REAL NOT NULL CHECK (amount_value > 0),
  amount_unit TEXT NOT NULL,
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_food_user_time ON food_entries(user_id, consumed_at);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER,
  action TEXT NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  ip_address TEXT,
  user_agent TEXT,
  details_json TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS login_throttle (
  throttle_key TEXT PRIMARY KEY,
  fail_count INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  endpoint TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  entity_type TEXT,
  entity_id INTEGER,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, endpoint, idempotency_key)
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id BLOB NOT NULL UNIQUE,
  public_key BLOB NOT NULL,
  sign_count INTEGER NOT NULL DEFAULT 0,
  rp_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_used_at TEXT
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DATABASE), exist_ok=True)
    conn = sqlite3.connect(DATABASE)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA journal_mode = WAL")

    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "is_admin" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    # Пользователь из ADMIN_USERNAME всегда получает права администратора
    # при каждом старте приложения. Это намеренный механизм восстановления
    # доступа (например, если admin-флаг был случайно снят), а не ошибка —
    # но учитывайте это при ротации ADMIN_USERNAME в окружении.
    conn.execute(
        "UPDATE users SET is_admin = 1 WHERE username = ?",
        (os.getenv("ADMIN_USERNAME", "admin"),),
    )

    username = os.getenv("ADMIN_USERNAME", "admin")
    password = os.getenv("ADMIN_PASSWORD")

    if username and password:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, display_name, status) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), username, "active"),
            )
        except sqlite3.IntegrityError:
            pass

    conn.commit()
    conn.close()


init_db()


def now_local():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value):
    if value is None or str(value).strip() == "":
        return now_local()

    value = str(value).strip().replace("T", " ")

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass

    raise ValueError("Некорректная дата/время")


def parse_float(value, min_value, max_value, field_name="Значение"):
    if value is None or str(value).strip() == "":
        raise ValueError(f"{field_name}: введите число")

    try:
        result = float(str(value).strip().replace(",", "."))
    except Exception:
        raise ValueError(f"{field_name}: введите число")

    if result < min_value or result > max_value:
        raise ValueError(f"{field_name}: допустимый диапазон {min_value}-{max_value}")

    return result


def parse_int(value, min_value, max_value, field_name="Значение", required=True):
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError(f"{field_name}: введите число")
        return None

    try:
        result = int(str(value).strip())
    except Exception:
        raise ValueError(f"{field_name}: введите целое число")

    if result < min_value or result > max_value:
        raise ValueError(f"{field_name}: допустимый диапазон {min_value}-{max_value}")

    return result


def parse_iso_date(value, default_date):
    if value is None or str(value).strip() == "":
        return default_date

    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError("Некорректная дата")


def audit(action, entity_type=None, entity_id=None, details=None):
    try:
        db = get_db()
        db.execute(
            "INSERT INTO audit_log (user_id, action, entity_type, entity_id, ip_address, user_agent, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session.get("user_id"),
                action,
                entity_type,
                entity_id,
                request.remote_addr,
                request.headers.get("User-Agent", "")[:255],
                json.dumps(details or {}, ensure_ascii=False),
            ),
        )
        db.commit()
    except Exception:
        pass


LOGIN_MAX_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCK_MINUTES = int(os.getenv("LOGIN_LOCK_MINUTES", "15"))


def throttle_key_for(username):
    # Ключ объединяет логин и IP: один заблокированный логин с одного IP
    # не блокирует того же пользователя при входе с другого адреса,
    # но не даёт перебирать пароли ни по логину, ни по IP отдельно.
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    return f"{(username or '').strip().lower()}|{ip}"


def is_login_locked(key):
    db = get_db()
    row = db.execute(
        "SELECT locked_until FROM login_throttle WHERE throttle_key = ?", (key,)
    ).fetchone()
    if not row or not row["locked_until"]:
        return False
    return row["locked_until"] > now_local()


def register_login_failure(key):
    db = get_db()
    row = db.execute(
        "SELECT fail_count FROM login_throttle WHERE throttle_key = ?", (key,)
    ).fetchone()
    fail_count = (row["fail_count"] if row else 0) + 1
    locked_until = None
    if fail_count >= LOGIN_MAX_ATTEMPTS:
        locked_until = (datetime.now() + timedelta(minutes=LOGIN_LOCK_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        fail_count = 0
    db.execute(
        """
        INSERT INTO login_throttle (throttle_key, fail_count, locked_until, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(throttle_key) DO UPDATE SET
          fail_count = excluded.fail_count,
          locked_until = excluded.locked_until,
          updated_at = datetime('now')
        """,
        (key, fail_count, locked_until),
    )
    db.commit()


def clear_login_failures(key):
    db = get_db()
    db.execute("DELETE FROM login_throttle WHERE throttle_key = ?", (key,))
    db.commit()


def get_idempotent_response(user_id, endpoint, idem_key):
    if not idem_key:
        return None
    db = get_db()
    row = db.execute(
        "SELECT response_json FROM idempotency_keys WHERE user_id = ? AND endpoint = ? AND idempotency_key = ?",
        (user_id, endpoint, idem_key),
    ).fetchone()
    return json.loads(row["response_json"]) if row else None


def store_idempotent_response(user_id, endpoint, idem_key, entity_type, entity_id, response_payload):
    if not idem_key:
        return
    db = get_db()
    try:
        db.execute(
            "INSERT INTO idempotency_keys (user_id, endpoint, idempotency_key, entity_type, entity_id, response_json) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, endpoint, idem_key, entity_type, entity_id, json.dumps(response_payload)),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Параллельный повтор того же запроса — уже сохранено другим потоком/запросом, это ок.
        db.rollback()


def wants_json_response():
    return request.path.startswith("/api/") or request.path == "/export.pdf"


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if wants_json_response():
                return jsonify(error="Требуется вход"), 401
            return redirect(url_for("login"))

        # Перепроверяем статус пользователя в БД на каждый запрос, чтобы
        # деактивация (в т.ч. через admin_delete_user) немедленно
        # прекращала доступ, а не только для новых входов в систему.
        db = get_db()
        row = db.execute("SELECT status FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not row or row["status"] != "active":
            session.clear()
            if wants_json_response():
                return jsonify(error="Учётная запись недоступна"), 401
            return redirect(url_for("login"))

        return f(*args, **kwargs)

    return wrapper


@app.before_request
def csrf_protect():
    if request.method in ("POST", "DELETE", "PUT", "PATCH") and request.path not in ("/login", "/api/webauthn/login/options", "/api/webauthn/login"):
        token = request.headers.get("X-CSRF-Token")

        if not token and request.is_json:
            data = request.get_json(silent=True) or {}
            token = data.get("csrf_token")

        session_token = session.get("csrf_token")
        if not session_token or not token or not secrets.compare_digest(str(token), str(session_token)):
            abort(403)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:;"
    )
    return response


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        tkey = throttle_key_for(username)

        if is_login_locked(tkey):
            audit("login_blocked", "user", None, {"username": username})
            error = f"Слишком много неудачных попыток. Повторите через {LOGIN_LOCK_MINUTES} мин."
            return render_template_string(LOGIN_HTML, error=error)

        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND status = ?",
            (username, "active"),
        ).fetchone()

        if user and check_password_hash(user["password_hash"], password):
            clear_login_failures(tkey)
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["display_name"] = user["display_name"] or user["username"]
            session["is_admin"] = 1 if user["is_admin"] else 0
            session["csrf_token"] = secrets.token_hex(32)
            session.permanent = True
            audit("login_success", "user", user["id"], {"username": username})
            return redirect(url_for("dashboard"))

        register_login_failure(tkey)
        audit("login_failed", "user", None, {"username": username})
        error = "Неверный логин или пароль"

    return render_template_string(LOGIN_HTML, error=error)


@app.post("/logout")
def logout():
    audit("logout")
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)

    return render_template_string(
        APP_HTML,
        csrf_token=session["csrf_token"],
        display_name=session.get("display_name") or session.get("username", ""),
        is_admin=1 if session.get("is_admin") else 0,
        user_id=session.get("user_id", 0),
    )


@app.post("/api/glucose")
@login_required
def api_glucose():
    data = request.get_json(silent=True) or {}
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    cached = get_idempotent_response(session["user_id"], "api_glucose", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        glucose_type = str(data.get("glucose_type", "")).strip()
        if glucose_type not in ("fasting", "post_meal"):
            raise ValueError("Выберите тип: натощак или после еды")

        value = parse_float(data.get("value"), 0.1, 100.0, "Глюкоза")
        measured_at = parse_dt(data.get("measured_at"))
        comment = str(data.get("comment") or "").strip()[:1000]

        db = get_db()
        cur = db.execute(
            "INSERT INTO glucose_entries (user_id, measured_at, glucose_type, value_mmol_l, comment, source) VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], measured_at, glucose_type, value, comment, "manual"),
        )
        db.commit()

        audit("create_glucose", "glucose_entries", cur.lastrowid, {"type": glucose_type})

        result = {"ok": True, "id": cur.lastrowid}
        store_idempotent_response(session["user_id"], "api_glucose", idem_key, "glucose_entries", cur.lastrowid, result)
        return jsonify(result)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/vitals")
@login_required
def api_vitals():
    data = request.get_json(silent=True) or {}
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    cached = get_idempotent_response(session["user_id"], "api_vitals", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        systolic = parse_int(data.get("systolic"), 30, 400, "Систолическое давление")
        diastolic = parse_int(data.get("diastolic"), 10, 300, "Диастолическое давление")
        pulse = parse_int(data.get("pulse"), 20, 300, "Пульс", required=False)
        measured_at = parse_dt(data.get("measured_at"))
        comment = str(data.get("comment") or "").strip()[:1000]

        if systolic <= diastolic:
            raise ValueError("Систолическое давление должно быть больше диастолического")

        db = get_db()
        cur = db.execute(
            "INSERT INTO blood_pressure_entries (user_id, measured_at, systolic_mmhg, diastolic_mmhg, pulse_bpm, comment, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                measured_at,
                systolic,
                diastolic,
                pulse,
                comment,
                "manual",
            ),
        )
        db.commit()

        audit("create_vitals", "blood_pressure_entries", cur.lastrowid)

        result = {"ok": True, "id": cur.lastrowid}
        store_idempotent_response(session["user_id"], "api_vitals", idem_key, "blood_pressure_entries", cur.lastrowid, result)
        return jsonify(result)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/food")
@login_required
def api_food():
    data = request.get_json(silent=True) or {}
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    cached = get_idempotent_response(session["user_id"], "api_food", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        food_name = str(data.get("food_name") or "").strip()[:150]
        if not food_name:
            raise ValueError("Укажите продукт")

        amount_value = parse_float(data.get("amount_value"), 0.01, 100000.0, "Количество")
        amount_unit = str(data.get("amount_unit") or "").strip()[:20]
        if not amount_unit:
            raise ValueError("Укажите единицу измерения")

        consumed_at = parse_dt(data.get("consumed_at"))
        comment = str(data.get("comment") or "").strip()[:1000]

        db = get_db()
        cur = db.execute(
            "INSERT INTO food_entries (user_id, food_name, consumed_at, amount_value, amount_unit, comment, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                session["user_id"],
                food_name,
                consumed_at,
                amount_value,
                amount_unit,
                comment,
                "manual",
            ),
        )
        db.commit()

        audit("create_food", "food_entries", cur.lastrowid)

        result = {"ok": True, "id": cur.lastrowid}
        store_idempotent_response(session["user_id"], "api_food", idem_key, "food_entries", cur.lastrowid, result)
        return jsonify(result)
    except ValueError as e:
        return jsonify(error=str(e)), 400


MAX_HISTORY_RANGE_DAYS = 366


def query_entries(user_id, date_from=None, date_to=None, entry_type="all", sort="date"):
    if entry_type not in ("all", "glucose", "vitals", "food"):
        raise ValueError("Некорректный тип фильтра")
    if sort not in ("date", "value"):
        raise ValueError("Некорректный порядок сортировки")

    today = date.today()
    default_from = today - timedelta(days=30)

    d_from_date = parse_iso_date(date_from, default_from)
    d_to_date = parse_iso_date(date_to, today)

    if d_from_date > d_to_date:
        raise ValueError("Начальная дата не может быть позже конечной")
    if (d_to_date - d_from_date).days > MAX_HISTORY_RANGE_DAYS:
        raise ValueError(f"Диапазон дат не может превышать {MAX_HISTORY_RANGE_DAYS} дней")

    d_from = d_from_date.isoformat()
    d_to = d_to_date.isoformat()

    db = get_db()
    entries = []

    if entry_type in ("all", "glucose"):
        rows = db.execute(
            """
            SELECT *
            FROM glucose_entries
            WHERE user_id = ?
              AND deleted_at IS NULL
              AND date(measured_at) >= date(?)
              AND date(measured_at) <= date(?)
            ORDER BY measured_at DESC
            """,
            (user_id, d_from, d_to),
        ).fetchall()

        for r in rows:
            glucose_type_label = "натощак" if r["glucose_type"] == "fasting" else "после еды"
            val = float(r["value_mmol_l"])
            low, high = REF_RANGES["glucose_fasting" if r["glucose_type"] == "fasting" else "glucose_post"]
            st = status_of(val, low, high)
            entries.append(
                {
                    "id": r["id"],
                    "type": "glucose",
                    "type_label": "Глюкоза",
                    "measured_at": r["measured_at"],
                    "display": f"{val:.1f} ммоль/л ({glucose_type_label})",
                    "display_html": f'<span class="st-{st}">{val:.1f}</span> ммоль/л ({glucose_type_label})',
                    "display_pdf": f'<font backcolor="{STATUS_COLORS[st]}">{val:.1f}</font> ммоль/л ({glucose_type_label})',
                    "comment": r["comment"] or "",
                    "sort_value": val,
                    "glucose_type": r["glucose_type"],
                    "value_mmol_l": val,
                }
            )

    if entry_type in ("all", "vitals"):
        rows = db.execute(
            """
            SELECT *
            FROM blood_pressure_entries
            WHERE user_id = ?
              AND deleted_at IS NULL
              AND date(measured_at) >= date(?)
              AND date(measured_at) <= date(?)
            ORDER BY measured_at DESC
            """,
            (user_id, d_from, d_to),
        ).fetchall()

        for r in rows:
            s = int(r["systolic_mmhg"])
            d = int(r["diastolic_mmhg"])
            p = r["pulse_bpm"]
            st_s = status_of(s, *REF_RANGES["systolic"])
            st_d = status_of(d, *REF_RANGES["diastolic"])
            display = f"{s}/{d} мм рт. ст."
            display_html = f'<span class="st-{st_s}">{s}</span>/<span class="st-{st_d}">{d}</span> мм рт. ст.'
            display_pdf = f'<font backcolor="{STATUS_COLORS[st_s]}">{s}</font>/<font backcolor="{STATUS_COLORS[st_d]}">{d}</font> мм рт. ст.'
            if p is not None:
                p = int(p)
                st_p = status_of(p, *REF_RANGES["pulse"])
                display += f", пульс {p}"
                display_html += f', пульс <span class="st-{st_p}">{p}</span>'
                display_pdf += f', пульс <font backcolor="{STATUS_COLORS[st_p]}">{p}</font>'
            entries.append(
                {
                    "id": r["id"],
                    "type": "vitals",
                    "type_label": "Давление/пульс",
                    "measured_at": r["measured_at"],
                    "display": display,
                    "display_html": display_html,
                    "display_pdf": display_pdf,
                    "comment": r["comment"] or "",
                    "sort_value": float(s),
                    "systolic_mmhg": s,
                    "diastolic_mmhg": d,
                    "pulse_bpm": p,
                }
            )

    if entry_type in ("all", "food"):
        rows = db.execute(
            """
            SELECT *
            FROM food_entries
            WHERE user_id = ?
              AND deleted_at IS NULL
              AND date(consumed_at) >= date(?)
              AND date(consumed_at) <= date(?)
            ORDER BY consumed_at DESC
            """,
            (user_id, d_from, d_to),
        ).fetchall()

        for r in rows:
            entries.append(
                {
                    "id": r["id"],
                    "type": "food",
                    "type_label": "Питание",
                    "measured_at": r["consumed_at"],
                    "display": f"{r['food_name']} — {float(r['amount_value']):g} {UNIT_RU.get(r['amount_unit'], r['amount_unit'])}",
                    "comment": r["comment"] or "",
                    "sort_value": float(r["amount_value"]),
                    "food_name": r["food_name"],
                    "amount_value": float(r["amount_value"]),
                    "amount_unit": r["amount_unit"],
                }
            )

    if sort == "value":
        entries.sort(key=lambda x: (x["type"], x["sort_value"]))
    else:
        entries.sort(key=lambda x: x["measured_at"], reverse=True)
    return entries, d_from, d_to


@app.get("/api/history")
@login_required
def api_history():
    try:
        entries, d_from, d_to = query_entries(
            session["user_id"],
            request.args.get("date_from"),
            request.args.get("date_to"),
            request.args.get("type", "all"),
            request.args.get("sort", "date"),
        )
        entries = add_assessments(entries)
        return jsonify(entries=entries, date_from=d_from, date_to=d_to)
    except ValueError as e:
        return jsonify(error=str(e)), 400


MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_day_ru(day_str):
    try:
        d = date.fromisoformat(day_str)
        return f"{d.day} {MONTHS_RU[d.month - 1]} {d.year} г."
    except ValueError:
        return day_str


def build_pdf(entries, d_from, d_to, sort="date", filter_label="Все записи", owner_name=""):
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="Медицинский дневник",
    )

    title_style = ParagraphStyle("Title", fontName=FONT_NAME, fontSize=14, leading=18, spaceAfter=4)
    normal_style = ParagraphStyle("Normal", fontName=FONT_NAME, fontSize=8, leading=10)
    day_style = ParagraphStyle("Day", fontName=FONT_NAME, fontSize=11, leading=14, spaceBefore=6, spaceAfter=3)
    header_style = ParagraphStyle("Header", fontName=FONT_NAME, fontSize=8, leading=10)
    cell_style = ParagraphStyle("Cell", fontName=FONT_NAME, fontSize=8, leading=10)

    elements = [
        Paragraph("Медицинский дневник", title_style),
        Paragraph(f"Период: {escape(d_from)} — {escape(d_to)}", normal_style),
        Paragraph(f"Пользователь: {escape(owner_name)}", normal_style),
        Paragraph(f"Фильтр: {escape(filter_label)}", normal_style),
        Paragraph("Подсветка: жёлтый — ниже нормы, зелёный — норма, красный — выше нормы. Статистические нормы, не диагноз.", normal_style),
        Paragraph("Данные введены пользователем и не являются медицинским заключением.", normal_style),
        Spacer(1, 6 * mm),
    ]

    if not entries:
        elements.append(Paragraph("Нет данных за выбранный период.", normal_style))
    else:
        days = {}
        for e in entries:
            days.setdefault(e["measured_at"][:10], []).append(e)

        for day in sorted(days.keys()):
            day_entries = days[day]
            if sort == "value":
                day_entries.sort(key=lambda x: (x["type"], x["sort_value"]))
            else:
                day_entries.sort(key=lambda x: x["measured_at"])

            elements.append(Paragraph(escape(format_day_ru(day)), day_style))

            data = [
                [
                    Paragraph("Время", header_style),
                 Paragraph("Тип", header_style),
                 Paragraph("Значение", header_style),
                 Paragraph("Оценка и рекомендация", header_style),
                 Paragraph("Комментарий", header_style),
                ]
            ]

            for e in day_entries:
                assessment_text = e.get("assessment") or ""
                recommendation_text = e.get("recommendation") or ""
                assessment_html = escape(assessment_text)
                if recommendation_text:
                    assessment_html += "<br/>" + escape(recommendation_text)
                data.append(
                    [
                        Paragraph(escape(e["measured_at"][11:16]), cell_style),
                        Paragraph(escape(e["type_label"]), cell_style),
                        Paragraph(e.get("display_pdf") or escape(e["display"]), cell_style),
                        Paragraph(assessment_html or "-", cell_style),
                        Paragraph(escape(e.get("comment") or ""), cell_style),
                    ]
                )

            table = Table(data, repeatRows=1, colWidths=[16 * mm, 22 * mm, 56 * mm, 54 * mm, 38 * mm])
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            elements.append(table)
            elements.append(Spacer(1, 4 * mm))

    doc.build(elements)
    buffer.seek(0)
    return buffer


@app.get("/export.pdf")
@login_required
def export_pdf():
    try:
        entries, d_from, d_to = query_entries(
            session["user_id"],
            request.args.get("date_from"),
            request.args.get("date_to"),
            request.args.get("type", "all"),
            request.args.get("sort", "date"),
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400

    entries = add_assessments(entries)
    type_label = {
        "all": "Все записи",
        "glucose": "Только глюкоза",
        "vitals": "Только давление и пульс",
        "food": "Только питание",
    }.get(request.args.get("type", "all"), "Все записи")

    db = get_db()
    owner = db.execute(
        "SELECT display_name, username FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    owner_name = (owner["display_name"] or owner["username"]) if owner else ""

    buffer = build_pdf(entries, d_from, d_to, request.args.get("sort", "date"), type_label, owner_name)

    audit("export_pdf", None, None, {"date_from": d_from, "date_to": d_to})

    filename = f"medical_diary_{d_from}_{d_to}.pdf"

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify(error="Требуется вход"), 401
        if not session.get("is_admin"):
            return jsonify(error="Недостаточно прав"), 403
        db = get_db()
        row = db.execute("SELECT status FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not row or row["status"] != "active":
            session.clear()
            return jsonify(error="Учётная запись недоступна"), 401
        return f(*args, **kwargs)

    return wrapper


@app.get("/api/admin/users")
@admin_required
def admin_list_users():
    db = get_db()
    rows = db.execute(
        "SELECT id, username, display_name, status, is_admin, created_at FROM users ORDER BY id"
    ).fetchall()
    return jsonify(users=[dict(r) for r in rows])


@app.post("/api/admin/users")
@admin_required
def admin_create_user():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    display_name = str(data.get("display_name") or "").strip()[:100]
    password = str(data.get("password") or "")

    if len(username) < 3 or len(username) > 64:
        return jsonify(error="Логин: от 3 до 64 символов"), 400
    if not display_name:
        return jsonify(error="Укажите отображаемое имя"), 400
    if len(password) < 8:
        return jsonify(error="Пароль: минимум 8 символов"), 400

    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, password_hash, display_name, status, is_admin) VALUES (?, ?, ?, 'active', 0)",
            (username, generate_password_hash(password), display_name),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="Пользователь с таким логином уже существует"), 400

    audit("admin_create_user", "users", cur.lastrowid, {"username": username})
    return jsonify(ok=True, id=cur.lastrowid)


@app.delete("/api/admin/users/<int:user_id>")
@admin_required
def admin_delete_user(user_id):
    # ВАЖНО: раньше запись пользователя удалялась физически (DELETE),
    # что каскадно (ON DELETE CASCADE) безвозвратно уничтожало все его
    # медицинские записи (глюкоза, давление, питание) без возможности
    # восстановления и без соблюдения требований к хранению медданных.
    # Теперь пользователь деактивируется (status='disabled'): вход
    # блокируется, но история наблюдений сохраняется для пациента,
    # аудита и последующего восстановления доступа при необходимости.
    db = get_db()
    target = db.execute(
        "SELECT username, is_admin, status FROM users WHERE id = ?", (user_id,)
    ).fetchone()

    if not target:
        return jsonify(error="Пользователь не найден"), 404
    if target["is_admin"]:
        return jsonify(error="Нельзя удалить пользователя с правами администратора"), 400
    if target["status"] == "disabled":
        return jsonify(error="Пользователь уже деактивирован"), 400

    db.execute(
        "UPDATE users SET status = 'disabled', updated_at = datetime('now') WHERE id = ?",
        (user_id,),
    )
    db.commit()

    audit("admin_deactivate_user", "users", user_id, {"username": target["username"]})
    return jsonify(ok=True)


@app.patch("/api/glucose/<int:entry_id>")
@login_required
def api_glucose_update(entry_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute(
        "SELECT * FROM glucose_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    try:
        glucose_type = str(data.get("glucose_type") or row["glucose_type"]).strip()
        if glucose_type not in ("fasting", "post_meal"):
            raise ValueError("Выберите тип: натощак или после еды")
        value = parse_float(data.get("value", row["value_mmol_l"]), 0.1, 100.0, "Глюкоза")
        measured_at = parse_dt(data.get("measured_at") or row["measured_at"])
        comment = str(data.get("comment") or "").strip()[:1000]
    except ValueError as e:
        return jsonify(error=str(e)), 400

    old = {"glucose_type": row["glucose_type"], "value_mmol_l": row["value_mmol_l"], "measured_at": row["measured_at"], "comment": row["comment"]}
    new = {"glucose_type": glucose_type, "value_mmol_l": value, "measured_at": measured_at, "comment": comment}

    db.execute(
        "UPDATE glucose_entries SET glucose_type = ?, value_mmol_l = ?, measured_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (glucose_type, value, measured_at, comment, entry_id),
    )
    db.commit()
    audit("update_glucose", "glucose_entries", entry_id, {"old": old, "new": new})
    return jsonify(ok=True)


@app.patch("/api/vitals/<int:entry_id>")
@login_required
def api_vitals_update(entry_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute(
        "SELECT * FROM blood_pressure_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    try:
        systolic = parse_int(data.get("systolic", row["systolic_mmhg"]), 30, 400, "Систолическое давление")
        diastolic = parse_int(data.get("diastolic", row["diastolic_mmhg"]), 10, 300, "Диастолическое давление")
        pulse = parse_int(data.get("pulse", row["pulse_bpm"]), 20, 300, "Пульс", required=False)
        measured_at = parse_dt(data.get("measured_at") or row["measured_at"])
        comment = str(data.get("comment") or "").strip()[:1000]
        if systolic <= diastolic:
            raise ValueError("Систолическое давление должно быть больше диастолического")
    except ValueError as e:
        return jsonify(error=str(e)), 400

    old = {"systolic_mmhg": row["systolic_mmhg"], "diastolic_mmhg": row["diastolic_mmhg"], "pulse_bpm": row["pulse_bpm"], "measured_at": row["measured_at"], "comment": row["comment"]}
    new = {"systolic_mmhg": systolic, "diastolic_mmhg": diastolic, "pulse_bpm": pulse, "measured_at": measured_at, "comment": comment}

    db.execute(
        "UPDATE blood_pressure_entries SET systolic_mmhg = ?, diastolic_mmhg = ?, pulse_bpm = ?, measured_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (systolic, diastolic, pulse, measured_at, comment, entry_id),
    )
    db.commit()
    audit("update_vitals", "blood_pressure_entries", entry_id, {"old": old, "new": new})
    return jsonify(ok=True)


@app.patch("/api/food/<int:entry_id>")
@login_required
def api_food_update(entry_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute(
        "SELECT * FROM food_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    try:
        food_name = str(data.get("food_name") or row["food_name"]).strip()[:150]
        if not food_name:
            raise ValueError("Укажите продукт")
        amount_value = parse_float(data.get("amount_value", row["amount_value"]), 0.01, 100000.0, "Количество")
        amount_unit = str(data.get("amount_unit") or row["amount_unit"]).strip()[:20]
        if not amount_unit:
            raise ValueError("Укажите единицу измерения")
        consumed_at = parse_dt(data.get("consumed_at") or row["consumed_at"])
        comment = str(data.get("comment") or "").strip()[:1000]
    except ValueError as e:
        return jsonify(error=str(e)), 400

    old = {"food_name": row["food_name"], "amount_value": row["amount_value"], "amount_unit": row["amount_unit"], "consumed_at": row["consumed_at"], "comment": row["comment"]}
    new = {"food_name": food_name, "amount_value": amount_value, "amount_unit": amount_unit, "consumed_at": consumed_at, "comment": comment}

    db.execute(
        "UPDATE food_entries SET food_name = ?, amount_value = ?, amount_unit = ?, consumed_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (food_name, amount_value, amount_unit, consumed_at, comment, entry_id),
    )
    db.commit()
    audit("update_food", "food_entries", entry_id, {"old": old, "new": new})
    return jsonify(ok=True)


@app.delete("/api/glucose/<int:entry_id>")
@login_required
def api_glucose_delete(entry_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM glucose_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    db.execute(
        "UPDATE glucose_entries SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (entry_id,),
    )
    db.commit()
    audit("delete_glucose", "glucose_entries", entry_id, {"old": {"glucose_type": row["glucose_type"], "value_mmol_l": row["value_mmol_l"], "measured_at": row["measured_at"], "comment": row["comment"]}})
    return jsonify(ok=True)


@app.delete("/api/vitals/<int:entry_id>")
@login_required
def api_vitals_delete(entry_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM blood_pressure_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    db.execute(
        "UPDATE blood_pressure_entries SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (entry_id,),
    )
    db.commit()
    audit("delete_vitals", "blood_pressure_entries", entry_id, {"old": {"systolic_mmhg": row["systolic_mmhg"], "diastolic_mmhg": row["diastolic_mmhg"], "pulse_bpm": row["pulse_bpm"], "measured_at": row["measured_at"], "comment": row["comment"]}})
    return jsonify(ok=True)


@app.delete("/api/food/<int:entry_id>")
@login_required
def api_food_delete(entry_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM food_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    db.execute(
        "UPDATE food_entries SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (entry_id,),
    )
    db.commit()
    audit("delete_food", "food_entries", entry_id, {"old": {"food_name": row["food_name"], "amount_value": row["amount_value"], "amount_unit": row["amount_unit"], "consumed_at": row["consumed_at"], "comment": row["comment"]}})
    return jsonify(ok=True)


@app.patch("/api/admin/users/<int:user_id>")
@admin_required
def admin_update_user(user_id):
    data = request.get_json(silent=True) or {}
    username = str(data.get("username") or "").strip()
    display_name = str(data.get("display_name") or "").strip()[:100]
    password = str(data.get("password") or "")

    if len(username) < 3 or len(username) > 64:
        return jsonify(error="Логин: от 3 до 64 символов"), 400
    if not display_name:
        return jsonify(error="Укажите отображаемое имя"), 400
    if password and len(password) < 8:
        return jsonify(error="Пароль: минимум 8 символов"), 400

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return jsonify(error="Пользователь не найден"), 404

    old = {"username": row["username"], "display_name": row["display_name"]}

    try:
        if password:
            db.execute(
                "UPDATE users SET username = ?, display_name = ?, password_hash = ?, updated_at = datetime('now') WHERE id = ?",
                (username, display_name, generate_password_hash(password), user_id),
            )
        else:
            db.execute(
                "UPDATE users SET username = ?, display_name = ?, updated_at = datetime('now') WHERE id = ?",
                (username, display_name, user_id),
            )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="Пользователь с таким логином уже существует"), 400

    audit("admin_update_user", "users", user_id, {"old": old, "new": {"username": username, "display_name": display_name, "password_changed": bool(password)}})
    return jsonify(ok=True)


def wa_rp():
    rp_id = os.getenv("WA_RP_ID", "").strip()
    origin = os.getenv("WA_ORIGIN", "").strip()
    if not rp_id:
        fwd_host = request.headers.get("X-Forwarded-Host", "")
        rp_id = (fwd_host.split(",")[0].strip() or request.host).split(":")[0]
    if not origin:
        proto = request.headers.get("X-Forwarded-Proto", "http")
        origin = f"{proto}://{rp_id}"
    return rp_id, origin


@app.post("/api/webauthn/register/options")
@login_required
def wa_register_options():
    if not WA_AVAILABLE:
        return jsonify(error="WebAuthn недоступен на сервере"), 501
    host, origin = wa_rp()
    print("WA register rp_id:", host, "origin:", origin, flush=True)
    if "." not in host and host != "localhost":
        return jsonify(error="WebAuthn: задайте WA_RP_ID и WA_ORIGIN в .env (домен HTTPS)"), 400
    db = get_db()
    rows = db.execute(
        "SELECT credential_id FROM webauthn_credentials WHERE user_id = ?",
        (session["user_id"],),
    ).fetchall()
    options = generate_registration_options(
        rp_id=host,
        rp_name="Медицинский дневник",
        user_id=str(session["user_id"]).encode(),
        user_name=session.get("username", ""),
        user_display_name=session.get("display_name", ""),
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            user_verification=UserVerificationRequirement.REQUIRED,
            resident_key=ResidentKeyRequirement.PREFERRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=r["credential_id"]) for r in rows],
    )
    session["wa_reg_challenge"] = bytes_to_base64url(options.challenge)
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.post("/api/webauthn/register")
@login_required
def wa_register():
    if not WA_AVAILABLE:
        return jsonify(error="WebAuthn недоступен на сервере"), 501
    host, origin = wa_rp()
    challenge_b64 = session.pop("wa_reg_challenge", None)
    if not challenge_b64:
        return jsonify(error="Сессия регистрации истекла, попробуйте снова"), 400
    try:
        credential = parse_registration_credential_json(request.get_data(as_text=True))
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=host,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception as e:
        return jsonify(error="Ошибка регистрации Face ID: %s" % e), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, rp_id, origin) VALUES (?, ?, ?, ?, ?, ?)",
            (session["user_id"], verification.credential_id, verification.credential_public_key, verification.sign_count, host, origin),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify(error="Этот ключ уже зарегистрирован"), 400

    audit("webauthn_register", "webauthn_credentials", None, {"rp_id": host})
    return jsonify(ok=True)


@app.delete("/api/webauthn/credentials")
@login_required
def wa_delete_all():
    db = get_db()
    db.execute("DELETE FROM webauthn_credentials WHERE user_id = ?", (session["user_id"],))
    db.commit()
    audit("webauthn_delete_all", "webauthn_credentials", None, {})
    return jsonify(ok=True)


@app.post("/api/webauthn/login/options")
def wa_login_options():
    if not WA_AVAILABLE:
        return jsonify(error="WebAuthn недоступен на сервере"), 501
    host, origin = wa_rp()
    print("WA login rp_id:", host, "origin:", origin, flush=True)
    if "." not in host and host != "localhost":
        return jsonify(error="WebAuthn: задайте WA_RP_ID и WA_ORIGIN в .env (домен HTTPS)"), 400
    options = generate_authentication_options(
        rp_id=host,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    session["wa_auth_challenge"] = bytes_to_base64url(options.challenge)
    return app.response_class(options_to_json(options), mimetype="application/json")


@app.post("/api/webauthn/login")
def wa_login():
    if not WA_AVAILABLE:
        return jsonify(error="WebAuthn недоступен на сервере"), 501

    tkey = throttle_key_for("webauthn:" + request.remote_addr if request.remote_addr else "webauthn")
    if is_login_locked(tkey):
        audit("login_blocked", "webauthn_credentials", None, {})
        return jsonify(error=f"Слишком много неудачных попыток. Повторите через {LOGIN_LOCK_MINUTES} мин."), 429

    challenge_b64 = session.pop("wa_auth_challenge", None)
    if not challenge_b64:
        return jsonify(error="Сессия входа истекла, попробуйте снова"), 400
    try:
        credential = parse_authentication_credential_json(request.get_data(as_text=True))
    except Exception:
        return jsonify(error="Некорректные данные входа"), 400

    db = get_db()
    row = db.execute(
        "SELECT * FROM webauthn_credentials WHERE credential_id = ?",
        (credential.raw_id,),
    ).fetchone()
    if not row:
        # Неизвестный credential_id — это НЕ признак перебора пароля (ID
        # непредсказуем и не подбирается), а обычно означает "осиротевший"
        # локальный passkey (например, ключи были удалены на сервере через
        # "Удалить все ключи", а в iCloud Keychain остались). Раз в счётчик
        # неудачных входов это писать не нужно — иначе автозапуск Face ID
        # при каждом визите на страницу входа мог бы залочить обычного
        # пользователя без единой реальной попытки подбора.
        return jsonify(error="Ключ не найден"), 404

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge_b64),
            expected_rp_id=row["rp_id"],
            expected_origin=row["origin"],
            credential_public_key=row["public_key"],
            credential_current_sign_count=row["sign_count"],
            require_user_verification=True,
        )
    except Exception:
        register_login_failure(tkey)
        audit("webauthn_login_failed", "webauthn_credentials", row["id"], {})
        return jsonify(error="Face ID не подтверждён"), 400

    db.execute(
        "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = datetime('now') WHERE id = ?",
        (verification.new_sign_count, row["id"]),
    )
    user = db.execute(
        "SELECT * FROM users WHERE id = ? AND status = 'active'",
        (row["user_id"],),
    ).fetchone()
    if not user:
        return jsonify(error="Пользователь неактивен"), 403

    clear_login_failures(tkey)
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["display_name"] = user["display_name"] or user["username"]
    session["is_admin"] = 1 if user["is_admin"] else 0
    session["csrf_token"] = secrets.token_hex(32)
    audit("login_webauthn", "user", user["id"], {"username": user["username"]})
    return jsonify(ok=True)


@app.errorhandler(400)
def bad_request_handler(e):
    if request.path.startswith("/api/") or request.path == "/export.pdf":
        return jsonify(error="Некорректный запрос"), 400
    return "Некорректный запрос", 400


@app.errorhandler(401)
def unauthorized_handler(e):
    if request.path.startswith("/api/") or request.path == "/export.pdf":
        return jsonify(error="Требуется вход"), 401
    return redirect(url_for("login"))


@app.errorhandler(403)
def forbidden_handler(e):
    if request.path.startswith("/api/") or request.path == "/export.pdf":
        return jsonify(error="Доступ запрещён"), 403
    return "Доступ запрещён", 403


@app.errorhandler(404)
def not_found_handler(e):
    if request.path.startswith("/api/") or request.path == "/export.pdf":
        return jsonify(error="Не найдено"), 404
    return "Не найдено", 404


LOGIN_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Медицинский дневник — вход</title>
  <link rel="apple-touch-icon" sizes="57x57" href="/apple-icon-57x57.png">
  <link rel="apple-touch-icon" sizes="60x60" href="/apple-icon-60x60.png">
  <link rel="apple-touch-icon" sizes="72x72" href="/apple-icon-72x72.png">
  <link rel="apple-touch-icon" sizes="76x76" href="/apple-icon-76x76.png">
  <link rel="apple-touch-icon" sizes="114x114" href="/apple-icon-114x114.png">
  <link rel="apple-touch-icon" sizes="120x120" href="/apple-icon-120x120.png">
  <link rel="apple-touch-icon" sizes="144x144" href="/apple-icon-144x144.png">
  <link rel="apple-touch-icon" sizes="152x152" href="/apple-icon-152x152.png">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-icon-180x180.png">
  <link rel="icon" type="image/png" sizes="192x192"  href="/android-icon-192x192.png">
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
  <link rel="icon" type="image/png" sizes="96x96" href="/favicon-96x96.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
  <link rel="manifest" href="/manifest.json">
  <meta name="msapplication-TileColor" content="#ffffff">
  <meta name="msapplication-TileImage" content="/ms-icon-144x144.png">
  <meta name="theme-color" content="#ffffff">
  <style>
    *, *::before, *::after {
      box-sizing: border-box;
    }
    html, body {
      overflow-x: hidden;
      max-width: 100vw;
    }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
      background: #f2f2f7;
      padding-top: env(safe-area-inset-top);
      padding-right: env(safe-area-inset-right);
      padding-bottom: env(safe-area-inset-bottom);
      padding-left: env(safe-area-inset-left);
      -webkit-text-size-adjust: 100%;
      overscroll-behavior-x: none;
    }
    main {
      max-width: 430px;
      margin: 0 auto;
      padding: 24px;
    }
    .card {
      background: #fff;
      border-radius: 18px;
      padding: 20px;
      margin-top: 32px;
    }
    .login-logo {
      display: block;
      width: 104px;
      height: 104px;
      object-fit: contain;
      margin: 0 auto 14px;
      border-radius: 24px;
    }
    h1 {
      font-size: 24px;
      margin: 0 0 16px;
    }
    label {
      display: block;
      margin: 12px 0 4px;
      font-size: 16px;
      color: #333;
    }
    input {
      width: 100%;
      min-height: 50px;
      border-radius: 14px;
      border: 1px solid #ccc;
      font-size: 20px;
      padding: 8px;
      box-sizing: border-box;
    }
    button {
      width: 100%;
      min-height: 52px;
      border: 0;
      border-radius: 14px;
      background: #007aff;
      color: #fff;
      font-size: 20px;
      margin-top: 16px;
    }
    .error {
      color: #d70015;
      margin-bottom: 10px;
    }

    /* Группа кнопок-переключателей (натощак / после еды) */
    .button-group {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      margin-top: 6px;
    }
    .button-group input[type="radio"] {
      position: absolute;
      opacity: 0;
      pointer-events: none;
      width: 1px;
      height: 1px;
    }
    .button-group label {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      min-height: 60px;
      margin: 0;
      border: 2px solid #d1d1d6;
      border-radius: 16px;
      background: #fff;
      font-size: 18px;
      font-weight: 500;
      color: #333;
      cursor: pointer;
      user-select: none;
      -webkit-tap-highlight-color: transparent;
      transition: background-color 0.15s, border-color 0.15s, color 0.15s;
      padding: 10px;
      text-align: center;
      line-height: 1.2;
      word-break: break-word;
    }
    .button-group label .emoji {
      font-size: 22px;
      line-height: 1;
    }
    .button-group input[type="radio"]:checked + label {
      background: #007aff;
      border-color: #007aff;
      color: #fff;
      box-shadow: 0 2px 8px rgba(0,122,255,0.25);
    }
    .button-group input[type="radio"]:focus-visible + label {
      outline: 3px solid rgba(0,122,255,0.4);
      outline-offset: 2px;
    }
  </style>
</head>
<body>
  <main>
    <div class="card">
      <img src="/logo.jpg" alt="Логотип" class="login-logo">
      <h1>Медицинский дневник</h1>
      {% if error %}<div class="error">{{ error }}</div>{% endif %}
      <form method="post">
        <label for="username">Логин</label>
        <input id="username" name="username" autocomplete="username" required>
        <label for="password">Пароль</label>
        <input id="password" type="password" name="password" autocomplete="current-password" required>
        <button type="submit">Войти</button>
        <button type="button" id="wa-login-btn" hidden onclick="waLogin()">🔐 Войти с биометрией</button>
        <div class="error" id="wa-login-msg"></div>
      </form>
    </div>
  </main>

  <script>
    function b64uToBuf(s) {
      s = s.replace(/-/g, '+').replace(/_/g, '/');
      while (s.length % 4) s += '=';
      var bin = atob(s);
      var buf = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return buf.buffer;
    }
    function bufToB64u(buf) {
      var b = new Uint8Array(buf);
      var s = '';
      for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    }
    function biometricName() {
      var ua = navigator.userAgent;
      if (/Android/i.test(ua)) return 'отпечатком пальца';
      var isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (isIOS) {
        var w = Math.min(screen.width, screen.height);
        var h = Math.max(screen.width, screen.height);
        if ((w === 320 && h === 568) || (w === 375 && h === 667)) return 'Touch ID';
        return 'Face ID';
      }
      return 'биометрией (Windows Hello / Touch ID)';
    }

    var WA_STORAGE_KEY = 'medical_diary_wa_credential_id';

    function getStoredCredentialId() {
      try { return localStorage.getItem(WA_STORAGE_KEY); } catch (e) { return null; }
    }
    function clearStoredCredentialId() {
      try { localStorage.removeItem(WA_STORAGE_KEY); } catch (e) {}
    }

    (function() {
      var btn = document.getElementById('wa-login-btn');
      var storedId = getStoredCredentialId();

      // Показываем кнопку и запускаем автовход ТОЛЬКО если на этом
      // устройстве вход по биометрии уже был включён в приложении
      // (сохранён идентификатор ключа после регистрации в настройках).
      // Наличие Face ID/Touch ID на самом устройстве ещё не означает,
      // что пользователь включил биометрический вход именно здесь —
      // раньше кнопка показывалась любому, у кого есть Face ID в iOS,
      // даже без зарегистрированного ключа.
      if (!storedId) { return; }
      if (!window.PublicKeyCredential || !PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable) { return; }

      PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().then(function(av) {
        if (av) {
          btn.textContent = '🔐 Войти: ' + biometricName();
          btn.hidden = false;
          // Автоматический запуск сразу при открытии страницы, без
          // нажатия кнопки. Safari (и другие современные браузеры)
          // разрешают один вызов navigator.credentials.get() без жеста
          // пользователя на каждую навигацию — именно для такого
          // сценария. Кнопка остаётся видимой как запасной вариант для
          // ручного повтора.
          waLogin(true);
        }
      }).catch(function() {});
    })();

    async function waLogin(silent) {
      try {
        var res = await fetch('/api/webauthn/login/options', { method: 'POST' });
        var opts = await res.json();
        if (!res.ok) throw new Error(opts.error || 'HTTP ' + res.status);
        opts.challenge = b64uToBuf(opts.challenge);

        var storedId = getStoredCredentialId();
        if (storedId) {
          // Явно указываем конкретный ключ этого устройства. Тогда
          // браузер/ОС находит его локально и сразу переходит к
          // разблокировке (Face ID/Touch ID) — без системного экрана
          // выбора "Использовать ключ входа / Другие параметры", который
          // иначе показывается при "безымянном" запросе без allowCredentials.
          opts.allowCredentials = [{ id: b64uToBuf(storedId), type: 'public-key' }];
        } else if (opts.allowCredentials) {
          opts.allowCredentials = opts.allowCredentials.map(function(c) { c.id = b64uToBuf(c.id); return c; });
        }

        var cred = await navigator.credentials.get({ publicKey: opts });
        var res2 = await fetch('/api/webauthn/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            id: cred.id,
            rawId: bufToB64u(cred.rawId),
            type: cred.type,
            response: {
              authenticatorData: bufToB64u(cred.response.authenticatorData),
              clientDataJSON: bufToB64u(cred.response.clientDataJSON),
              signature: bufToB64u(cred.response.signature),
              userHandle: cred.response.userHandle ? bufToB64u(cred.response.userHandle) : null
            }
          })
        });
        var out = await res2.json();
        if (!res2.ok) {
          if (res2.status === 404) {
            // Сервер не знает такой ключ (например, вход по биометрии
            // был отключён) — локальная подсказка устарела, чистим её.
            clearStoredCredentialId();
          }
          throw new Error(out.error || 'HTTP ' + res2.status);
        }
        window.location = '/';
      } catch (err) {
        // В "тихом" автозапуске ничего не показываем: пользователь мог
        // отменить системный диалог, или ключ не подошёл — это штатная
        // ситуация, а не ошибка. Форма логина/пароля остаётся доступной.
        // При ручном нажатии кнопки (silent не передан) ошибку показываем,
        // чтобы пользователь понимал, что пошло не так.
        console.log('WebAuthn login attempt failed:', err && err.message);
        if (!silent) {
          var el = document.getElementById('wa-login-msg');
          el.textContent = err.message;
        }
      }
    }
  </script>
</body>
</html>
"""


APP_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="csrf-token" content="{{ csrf_token }}">
  <title>Медицинский дневник</title>
  <link rel="apple-touch-icon" sizes="57x57" href="/apple-icon-57x57.png">
  <link rel="apple-touch-icon" sizes="60x60" href="/apple-icon-60x60.png">
  <link rel="apple-touch-icon" sizes="72x72" href="/apple-icon-72x72.png">
  <link rel="apple-touch-icon" sizes="76x76" href="/apple-icon-76x76.png">
  <link rel="apple-touch-icon" sizes="114x114" href="/apple-icon-114x114.png">
  <link rel="apple-touch-icon" sizes="120x120" href="/apple-icon-120x120.png">
  <link rel="apple-touch-icon" sizes="144x144" href="/apple-icon-144x144.png">
  <link rel="apple-touch-icon" sizes="152x152" href="/apple-icon-152x152.png">
  <link rel="apple-touch-icon" sizes="180x180" href="/apple-icon-180x180.png">
  <link rel="icon" type="image/png" sizes="192x192"  href="/android-icon-192x192.png">
  <link rel="icon" type="image/png" sizes="32x32" href="/favicon-32x32.png">
  <link rel="icon" type="image/png" sizes="96x96" href="/favicon-96x96.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16x16.png">
  <link rel="manifest" href="/manifest.json">
  <meta name="msapplication-TileColor" content="#ffffff">
  <meta name="msapplication-TileImage" content="/ms-icon-144x144.png">
  <meta name="theme-color" content="#ffffff">
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    html { overflow-x: hidden; }
    body {
      margin: 0;
      background: #f2f2f7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      -webkit-text-size-adjust: 100%;
      overflow-x: hidden;
      max-width: 100vw;
      padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
      overscroll-behavior-x: none;
    }
    header {
      position: sticky; top: 0; z-index: 10;
      background: #fff; border-bottom: 1px solid #ddd;
      padding: 10px 14px;
      display: flex; justify-content: space-between; align-items: center; gap: 8px;
    }
    header .title { font-size: 20px; font-weight: 700; flex: 0 0 auto; }
    header .user {
      color: #666; font-size: 15px; font-weight: 600; flex: 1 1 auto; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-align: center;
    }
    header button {
      flex: 0 0 auto; width: auto; min-height: 44px;
      padding: 0 14px; font-size: 16px; margin-top: 0; line-height: 44px;
    }
    main { padding: 14px; padding-bottom: 110px; max-width: 760px; margin: 0 auto; width: 100%; }
    .card { background: #fff; border-radius: 18px; padding: 14px; margin-bottom: 14px; overflow: hidden; }
    summary { font-size: 21px; font-weight: 600; cursor: pointer; padding: 4px 0; }
    label { display: block; margin: 12px 0 4px; font-size: 16px; color: #333; }
    input, select, textarea {
      display: block; width: 100%; max-width: 100%; min-height: 48px;
      border-radius: 14px; border: 1px solid #ccc; font-size: 20px;
      padding: 8px 10px; background: #fff;
    }
    textarea { min-height: 70px; }
    input[type="date"], input[type="datetime-local"] {
      -webkit-appearance: none;
      appearance: none;
      min-width: 0;
      width: 100%;
      max-width: 100%;
      font-size: 17px;
      padding: 10px 8px;
      white-space: nowrap;
      overflow: hidden;
    }
    button, .button {
      display: block; width: 100%; max-width: 100%; min-height: 52px;
      border: 0; border-radius: 14px; background: #007aff; color: #fff;
      font-size: 20px; text-align: center; text-decoration: none;
      line-height: 52px; margin-top: 14px; cursor: pointer;
    }
    button.danger { background: #d70015; min-height: 44px; line-height: 44px; font-size: 16px; margin-top: 0; }
    button.secondary { background: #8e8e93; }
    button.edit-btn { width: auto; min-height: 40px; line-height: 40px; font-size: 16px; margin-top: 0; padding: 0 12px; }
    button.del-btn { width: auto; min-height: 40px; line-height: 40px; font-size: 16px; margin-top: 0; padding: 0 10px; background: #d70015; margin-left: 6px; }
    .cell-actions { display: flex; gap: 6px; }
    .cell-actions button { margin-left: 0; }
    [hidden] { display: none !important; }
    #pdf-overlay {
      position: fixed; inset: 0; z-index: 100;
      background: #525659;
      display: flex; flex-direction: column;
      padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
    }
    .pdf-toolbar { display: flex; align-items: center; gap: 10px; background: #fff; padding: 10px 12px; }
    .pdf-title { flex: 1; min-width: 0; font-weight: 700; font-size: 17px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pdf-btn { width: auto; min-height: 44px; line-height: 44px; margin: 0; padding: 0 14px; font-size: 20px; }
    #zoom-label { min-width: 52px; text-align: center; font-weight: 700; font-size: 15px; color: #333; }
    #pdf-pages { flex: 1; overflow: auto; -webkit-overflow-scrolling: touch; padding: 10px; }
    #pdf-pages canvas { display: block; margin: 0 auto 10px; background: #fff; border-radius: 4px; box-shadow: 0 1px 4px rgba(0,0,0,.4); }
    .pdf-status { color: #fff; text-align: center; padding: 24px; font-size: 16px; }
    .edit-title { font-size: 21px; font-weight: 600; padding: 4px 0; }
    .row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .row > div { min-width: 0; }
    .message { margin-top: 10px; font-size: 16px; }
    .ok { color: #1a7f37; }
    .error { color: #d70015; }
    .table-wrap { overflow-x: auto; max-width: 100%; -webkit-overflow-scrolling: touch; }
    table { width: 100%; border-collapse: collapse; font-size: 14px; margin-top: 12px; }
    th, td { border: 1px solid #ddd; padding: 6px; text-align: left; vertical-align: top; word-break: break-word; }
    .muted { color: #666; font-size: 14px; }
    .button-group { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 6px; }
    .button-group input { position: absolute; opacity: 0; pointer-events: none; width: 1px; height: 1px; }
    .button-group label {
      display: flex; align-items: center; justify-content: center; gap: 8px;
      min-height: 60px; margin: 0; padding: 8px;
      border: 2px solid #d1d1d6; border-radius: 16px; background: #fff;
      font-size: 18px; font-weight: 600; color: #333; cursor: pointer;
      -webkit-tap-highlight-color: transparent; text-align: center; line-height: 1.15;
    }
    .button-group .emoji { font-size: 22px; line-height: 1; }
    .button-group input:checked + label { background: #007aff; border-color: #007aff; color: #fff; }
    .button-group.cols3 { grid-template-columns: 1fr 1fr 1fr; }
    .button-group.cols3 label { font-size: 15px; min-height: 52px; }
    .st-low, .st-ok, .st-high { padding: 2px 6px; border-radius: 8px; font-weight: 700; }
    .st-low { background: #fff3c4; }
    .st-ok { background: #d9f2d9; }
    .st-high { background: #fbd9d9; }
    .legend { margin-top: 10px; font-size: 13px; color: #666; line-height: 1.6; }
    .legend .dot { display: inline-block; width: 12px; height: 12px; border-radius: 4px; vertical-align: -1px; }
    .date-btn {
      position: relative; display: flex; align-items: center; gap: 8px;
      min-height: 52px; margin: 0; padding: 8px 12px;
      border: 2px solid #d1d1d6; border-radius: 16px; background: #fff;
      font-size: 18px; font-weight: 600; color: #333; cursor: pointer;
      -webkit-tap-highlight-color: transparent;
    }
    .date-btn .emoji { font-size: 22px; line-height: 1; }
    .date-btn .date-val { color: #007aff; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .date-btn input {
      position: absolute; inset: 0; width: 100%; height: 100%;
      opacity: 0; margin: 0; padding: 0; border: 0; min-height: 0; cursor: pointer;
    }
  
.about-note { font-size: 15px; line-height: 1.45; color: #1c1c1e; }
.about-note h3 { margin: 16px 0 6px; font-size: 17px; }
.about-note p, .about-note li { margin: 6px 0; }
.about-note ul, .about-note ol { margin-left: 18px; padding-left: 6px; }
.recommendation { margin-top: 4px; font-size: 12px; line-height: 1.35; color: #666; }
.assess-cell { min-width: 150px; }

</style>
</head>
<body>
  <header>
    <span class="title">Дневник</span>
    <span class="user">{{ display_name }}</span>
    <button type="button" onclick="logout()">Выход</button>
  </header>

  <main>
    <div class="card" id="edit-card" hidden>
      <div class="edit-title" id="edit-title">✏️ Редактирование</div>

      <div id="edit-glucose" hidden>
        <label>Тип измерения</label>
        <select id="edit_glucose_type">
          <option value="fasting">🌅 Натощак</option>
          <option value="post_meal">🍽️ После еды</option>
        </select>
        <label>Значение, ммоль/л</label>
        <input id="edit_glucose_value" type="number" step="0.1" min="0.1" max="100" inputmode="decimal">
      </div>

      <div id="edit-vitals" hidden>
        <div class="row">
          <div>
            <label>Систолическое</label>
            <input id="edit_systolic" type="number" min="30" max="400" inputmode="numeric">
          </div>
          <div>
            <label>Диастолическое</label>
            <input id="edit_diastolic" type="number" min="10" max="300" inputmode="numeric">
          </div>
        </div>
        <label>Пульс</label>
        <input id="edit_pulse" type="number" min="20" max="300" inputmode="numeric">
      </div>

      <div id="edit-food" hidden>
        <label>Продукт</label>
        <input id="edit_food_name" maxlength="150">
        <div class="row">
          <div>
            <label>Количество</label>
            <input id="edit_amount_value" type="number" step="0.01" min="0.01" inputmode="decimal">
          </div>
          <div>
            <label>Единица</label>
            <select id="edit_amount_unit">
              <option value="г">г</option>
              <option value="мл">мл</option>
              <option value="шт">шт</option>
              <option value="порция">порция</option>
            </select>
          </div>
        </div>
      </div>

      <label>Дата и время</label>
      <input id="edit_measured_at" type="datetime-local">

      <label>Комментарий</label>
      <textarea id="edit_comment" maxlength="1000"></textarea>

      <button type="button" onclick="saveEdit()">Сохранить изменения</button>
      <button type="button" class="secondary" onclick="closeEdit()">Отмена</button>
      <div class="message" id="edit-msg"></div>
    </div>

    <div class="card">
      <details>
        <summary>🩸 Глюкоза</summary>
        <form id="glucose-form">
          <label>Тип измерения</label>
          <div class="button-group" role="radiogroup" aria-label="Тип измерения глюкозы">
            <input type="radio" id="gt_fasting" name="glucose_type" value="fasting" checked>
            <label for="gt_fasting"><span class="emoji">🌅</span><span>Натощак</span></label>
            <input type="radio" id="gt_postmeal" name="glucose_type" value="post_meal">
            <label for="gt_postmeal"><span class="emoji">🍽️</span><span>После еды</span></label>
          </div>

          <label>Значение, ммоль/л</label>
          <input name="value" type="number" step="0.1" min="0.1" max="100" inputmode="decimal" required>

          <label>Дата и время</label>
          <input name="measured_at" type="datetime-local" class="dt">

          <label>Комментарий</label>
          <textarea name="comment" maxlength="1000"></textarea>

          <button type="submit">Сохранить</button>
          <div class="message" id="glucose-msg"></div>
        </form>
      </details>
    </div>

    <div class="card">
      <details>
        <summary>💓 Давление и пульс</summary>
        <form id="vitals-form">
          <div class="row">
            <div>
              <label>Систолическое</label>
              <input name="systolic" type="number" min="30" max="400" inputmode="numeric" required>
            </div>
            <div>
              <label>Диастолическое</label>
              <input name="diastolic" type="number" min="10" max="300" inputmode="numeric" required>
            </div>
          </div>

          <label>Пульс</label>
          <input name="pulse" type="number" min="20" max="300" inputmode="numeric">

          <label>Дата и время</label>
          <input name="measured_at" type="datetime-local" class="dt">

          <label>Комментарий</label>
          <textarea name="comment" maxlength="1000"></textarea>

          <button type="submit">Сохранить</button>
          <div class="message" id="vitals-msg"></div>
        </form>
      </details>
    </div>

    <div class="card">
      <details>
        <summary>🥗 Питание</summary>
        <form id="food-form">
          <label>Продукт</label>
          <input name="food_name" maxlength="150" required>

          <div class="row">
            <div>
              <label>Количество</label>
              <input name="amount_value" type="number" step="0.01" min="0.01" inputmode="decimal" required>
            </div>
            <div>
              <label>Единица</label>
              <select name="amount_unit">
                <option value="г">г</option>
                <option value="мл">мл</option>
                <option value="шт">шт</option>
                <option value="порция">порция</option>
              </select>
            </div>
          </div>

          <label>Дата и время</label>
          <input name="consumed_at" type="datetime-local" class="dt">

          <label>Комментарий</label>
          <textarea name="comment" maxlength="1000"></textarea>

          <button type="submit">Сохранить</button>
          <div class="message" id="food-msg"></div>
        </form>
      </details>
    </div>

    <div class="card">
      <details>
        <summary>📋 История</summary>

        <div class="row">
          <div>
            <span class="date-btn">
              <span class="emoji">📅</span><span>с</span>
              <span class="date-val" id="date_from_label"></span>
              <input type="date" id="date_from" aria-label="Дата начала периода">
            </span>
          </div>
          <div>
            <span class="date-btn">
              <span class="emoji">📅</span><span>по</span>
              <span class="date-val" id="date_to_label"></span>
              <input type="date" id="date_to" aria-label="Дата конца периода">
            </span>
          </div>
        </div>

        <label for="history_type">Показывать</label>
        <select id="history_type" name="history_type">
          <option value="all">📋 Все записи</option>
          <option value="glucose">🩸 Только глюкоза</option>
          <option value="vitals">💓 Только давление и пульс</option>
          <option value="food">🥗 Только питание</option>
        </select>

        <label>Сортировка</label>
        <div class="button-group" role="radiogroup" aria-label="Сортировка">
          <input type="radio" id="hs_date" name="history_sort" value="date" checked>
          <label for="hs_date"><span class="emoji">📅</span><span>По дате</span></label>
          <input type="radio" id="hs_value" name="history_sort" value="value">
          <label for="hs_value"><span class="emoji">🔢</span><span>По значению</span></label>
        </div>

        <button type="button" id="export-btn" class="button" onclick="openPdfViewer()">📄 Выгрузить PDF</button>

        <div class="legend">
          <span class="dot" style="background:#fff3c4"></span> ниже нормы ·
          <span class="dot" style="background:#d9f2d9"></span> норма ·
          <span class="dot" style="background:#fbd9d9"></span> выше нормы.<br>
          Подсветка опирается на общестатистические нормы и не является диагнозом.
        </div>

        <div class="message" id="history-msg"></div>

        <div class="table-wrap">
          <table id="history-table">
            <thead>
              <tr>
                <th>Дата</th>
                <th>Тип</th>
                <th>Значение</th>
                <th>Оценка</th>
             <th>Комментарий</th>
                <th></th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </details>
    </div>

    
<div class="card">
  <details>
    <summary>ℹ️ О программе</summary>
    <div class="about-note">
      <p><strong>Медицинский дневник</strong> — сервис для хранения и наблюдения показателей: глюкоза, давление, пульс и питание.</p>
      <p><strong>Важно:</strong> приложение не ставит диагнозы и не назначает лечение. Автоматические оценки — справочные.</p>
      <h3>Как пользоваться</h3>
      <ol>
        <li>Войдите в приложение.</li>
        <li>Добавьте измерение: глюкоза, давление/пульс или питание.</li>
        <li>Проверьте записи в разделе «История».</li>
        <li>При необходимости редактируйте или удалите запись.</li>
        <li>Выгрузите PDF для врача.</li>
      </ol>
      <h3>Как вводить данные</h3>
      <ul>
        <li>Глюкоза: выберите тип — натощак или после еды, укажите значение в ммоль/л.</li>
        <li>Давление: укажите систолическое и диастолическое значение, пульс — при наличии.</li>
        <li>Питание: продукт, количество и единицу измерения. Неточное количество лучше указать приблизительно и написать комментарий.</li>
        <li>В комментарии полезно указывать самочувствие, еду, нагрузку и другие факторы.</li>
      </ul>
      <h3>Установка как веб-приложение</h3>
      <ul>
        <li>iPhone/iPad, Safari: «Поделиться» → «На экран „Домой»».</li>
        <li>Android, Chrome: меню → «Добавить на главный экран» или «Установить приложение».</li>
        <li>Компьютер, Chrome/Edge: значок установки в адресной строке или меню → «Установить».</li>
      </ul>
      <h3>Рекомендации по измерению</h3>
      <ul>
        <li>Глюкоза: чистые сухие руки; при неожиданном значении повторите измерение.</li>
        <li>Давление: сидя, после 5 минут покоя, манжета на уровне сердца, не разговаривать.</li>
        <li>Пульс: измеряйте в покое; при необычных значениях повторите позже.</li>
        <li>Регулярность важнее идеальной точности.</li>
      </ul>
      <h3>Безопасность</h3>
      <ul>
        <li>Не сообщайте пароль другим людям.</li>
        <li>На чужом устройстве выходите из приложения.</li>
        <li>Регулярно сохраняйте PDF и резервные копии базы данных.</li>
      </ul>
    </div>
  </details>
</div>

 <div class="card">
      <details>
        <summary id="wa-summary">🔐 Биометрия</summary>
        <p class="muted" id="wa-hint">Быстрый вход по биометрии этого устройства, без пароля.</p>
        <button type="button" id="wa-enable-btn" onclick="waRegister()">Включить</button>
        <button type="button" class="secondary" onclick="waDelete()">Отключить</button>
        <p class="muted" id="wa-availability"></p>
        <div class="message" id="wa-msg"></div>
      </details>
    </div>

    {% if is_admin %}
    <div class="card">
      <details>
        <summary>👥 Пользователи</summary>

        <form id="user-form">
          <label>Логин</label>
          <input name="username" maxlength="64" autocomplete="off" required>

          <label>Отображаемое имя</label>
          <input name="display_name" maxlength="100" required>

          <label>Пароль</label>
          <input name="password" type="password" minlength="8" autocomplete="new-password" required>

          <button type="submit">Добавить пользователя</button>
          <div class="message" id="user-msg"></div>
        </form>

        <form id="user-edit-form" hidden>
          <div class="edit-title" id="user-edit-title">✏️ Редактирование пользователя</div>

          <label>Логин</label>
          <input name="username" maxlength="64" autocomplete="off" required>

          <label>Отображаемое имя</label>
          <input name="display_name" maxlength="100" required>

          <label>Новый пароль (не менять — оставьте пустым)</label>
          <input name="password" type="password" minlength="8" autocomplete="new-password">

          <button type="submit">Сохранить</button>
          <button type="button" class="secondary" onclick="cancelUserEdit()">Отмена</button>
          <div class="message" id="user-edit-msg"></div>
        </form>

        <div class="table-wrap">
          <table id="users-table">
            <thead>
              <tr>
                <th>Имя</th>
                <th>Логин</th>
                <th></th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
      </details>
    </div>
    {% endif %}
  </main>

  <div id="pdf-overlay" hidden>
    <div class="pdf-toolbar">
      <button type="button" class="pdf-btn" onclick="zoomPdf(-1)" aria-label="Уменьшить">➖</button>
      <span id="zoom-label">100%</span>
      <button type="button" class="pdf-btn" onclick="zoomPdf(1)" aria-label="Увеличить">➕</button>
      <span class="pdf-title">📄</span>
      <button type="button" class="pdf-btn" onclick="sharePdf()" aria-label="Поделиться PDF">📤</button>
      <button type="button" class="pdf-btn" onclick="closePdfViewer()" aria-label="Закрыть просмотр">❌</button>
    </div>
    <div id="pdf-pages"></div>
  </div>

  <script src="/pdf.min.js"></script>

  <script>
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var IS_ADMIN = {{ is_admin }};
    var USER_ID = {{ user_id }};

    function pad(n) { return String(n).padStart(2, '0'); }

    function localDateTime() {
      var d = new Date();
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    function localDate(offsetDays) {
      var d = new Date();
      d.setDate(d.getDate() + offsetDays);
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
    }

    function fmtDateLabel(v) {
      if (!v) return '';
      var p = v.split('-');
      return p[2] + '.' + p[1] + '.' + p[0];
    }

    function updateDateLabels() {
      document.getElementById('date_from_label').textContent = fmtDateLabel(document.getElementById('date_from').value);
      document.getElementById('date_to_label').textContent = fmtDateLabel(document.getElementById('date_to').value);
    }

    document.querySelectorAll('.dt').forEach(function(el) { el.value = localDateTime(); });
    document.getElementById('date_from').value = localDate(-7);
    document.getElementById('date_to').value = localDate(0);
    document.getElementById('date_from').addEventListener('change', function() { updateDateLabels(); loadHistory(); });
    document.getElementById('date_to').addEventListener('change', function() { updateDateLabels(); loadHistory(); });
    updateDateLabels();

    document.getElementById('history_type').addEventListener('change', loadHistory);
    document.querySelectorAll('input[name="history_sort"]').forEach(function(el) {
      el.addEventListener('change', loadHistory);
    });

    function setMsg(id, text, ok) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'message ' + (ok ? 'ok' : 'error');
    }

    async function postJSON(url, data) {
      var res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(data)
      });

      if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }

      var out = {};
      try { out = await res.json(); } catch (e) {}

      if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
      return out;
    }

    document.getElementById('glucose-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      try {
        await postJSON('/api/glucose', {
          glucose_type: f.glucose_type.value,
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        });
        setMsg('glucose-msg', 'Сохранено', true);
        f.value.value = '';
        f.comment.value = '';
        loadHistory();
      } catch (err) { setMsg('glucose-msg', err.message, false); }
    });

    document.getElementById('vitals-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      try {
        await postJSON('/api/vitals', {
          systolic: f.systolic.value,
          diastolic: f.diastolic.value,
          pulse: f.pulse.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        });
        setMsg('vitals-msg', 'Сохранено', true);
        f.comment.value = '';
        loadHistory();
      } catch (err) { setMsg('vitals-msg', err.message, false); }
    });

    document.getElementById('food-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      try {
        await postJSON('/api/food', {
          food_name: f.food_name.value,
          amount_value: f.amount_value.value,
          amount_unit: f.amount_unit.value,
          consumed_at: f.consumed_at.value.replace('T', ' '),
          comment: f.comment.value
        });
        setMsg('food-msg', 'Сохранено', true);
        f.food_name.value = '';
        f.amount_value.value = '';
        f.comment.value = '';
        loadHistory();
      } catch (err) { setMsg('food-msg', err.message, false); }
    });

    function historyParams() {
      return {
        df: document.getElementById('date_from').value,
        dt: document.getElementById('date_to').value,
        type: document.getElementById('history_type').value,
        sort: document.querySelector('input[name="history_sort"]:checked').value
      };
    }

    async function loadHistory() {
      var hp = historyParams();
      var df = hp.df;
      var dt = hp.dt;
      try {
        var res = await fetch('/api/history?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort));
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        var tbody = document.querySelector('#history-table tbody');
        tbody.innerHTML = '';
        out.entries.forEach(function(entry) {
          var tr = document.createElement('tr');

          var td1 = document.createElement('td');
          td1.textContent = entry.measured_at;
          tr.appendChild(td1);

          var td2 = document.createElement('td');
          td2.textContent = entry.type_label;
          tr.appendChild(td2);

          var td3 = document.createElement('td');
          if (entry.display_html) { td3.innerHTML = entry.display_html; } else { td3.textContent = entry.display; }
          tr.appendChild(td3);

          var tdAssess = document.createElement('td');
          tdAssess.className = 'assess-cell';
          if (entry.assessment) {
            var assessBadge = document.createElement('span');
            assessBadge.className = 'st-' + (entry.assessment_status || 'ok');
            assessBadge.textContent = entry.assessment;
            tdAssess.appendChild(assessBadge);
            if (entry.recommendation) {
              var recEl = document.createElement('div');
              recEl.className = 'recommendation';
              recEl.textContent = entry.recommendation;
              tdAssess.appendChild(recEl);
            }
          }
          var td4 = document.createElement('td');
          td4.textContent = entry.comment || '';
          tr.appendChild(td4);

          var td5 = document.createElement('td');
          var eb = document.createElement('button');
          eb.type = 'button';
          eb.className = 'edit-btn';
          eb.textContent = '✏️';
          eb.setAttribute('aria-label', 'Редактировать запись');
          eb.addEventListener('click', function() { startEdit(entry); });
          td5.appendChild(eb);

          var dbtn = document.createElement('button');
          dbtn.type = 'button';
          dbtn.className = 'del-btn';
          dbtn.textContent = '🗑️';
          dbtn.setAttribute('aria-label', 'Удалить запись');
          dbtn.addEventListener('click', function() { deleteEntry(entry); });
          td5.appendChild(dbtn);

          tr.appendChild(td5);

          tbody.appendChild(tr);
        });

        currentExportUrl = '/export.pdf?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort);
        setMsg('history-msg', 'Записей: ' + out.entries.length, true);
      } catch (err) { setMsg('history-msg', err.message, false); }
    }

    async function loadUsers() {
      if (!IS_ADMIN) return;
      try {
        var res = await fetch('/api/admin/users');
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        var tbody = document.querySelector('#users-table tbody');
        tbody.innerHTML = '';
        out.users.forEach(function(u) {
          var tr = document.createElement('tr');

          var td1 = document.createElement('td');
          td1.textContent = u.display_name || u.username;
          tr.appendChild(td1);

          var td2 = document.createElement('td');
          td2.textContent = u.username + (u.is_admin ? ' (админ)' : '');
          tr.appendChild(td2);

          var td3 = document.createElement('td');
          var wrap = document.createElement('div');
          wrap.className = 'cell-actions';

          var eb2 = document.createElement('button');
          eb2.type = 'button';
          eb2.className = 'edit-btn';
          eb2.textContent = '✏️';
          eb2.setAttribute('aria-label', 'Редактировать пользователя');
          eb2.addEventListener('click', function() { editUser(u); });
          wrap.appendChild(eb2);

          if (!u.is_admin) {
            var b = document.createElement('button');
            b.type = 'button';
            b.className = 'del-btn';
            b.textContent = '🗑️';
            b.setAttribute('aria-label', 'Удалить пользователя');
            b.addEventListener('click', function() { deleteUser(u.id, u.username); });
            wrap.appendChild(b);
          }

          td3.appendChild(wrap);
          tr.appendChild(td3);

          tbody.appendChild(tr);
        });
      } catch (err) { setMsg('user-msg', err.message, false); }
    }

    var userEditId = null;

    function editUser(u) {
      userEditId = u.id;
      var f = document.getElementById('user-edit-form');
      f.hidden = false;
      f.username.value = u.username;
      f.display_name.value = u.display_name || '';
      f.password.value = '';
      document.getElementById('user-edit-title').textContent = '✏️ ' + (u.display_name || u.username);
      setMsg('user-edit-msg', '', true);
      f.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function cancelUserEdit() {
      userEditId = null;
      document.getElementById('user-edit-form').hidden = true;
    }

    async function deleteUser(id, name) {
      if (!confirm('Удалить пользователя ' + name + ' и все его записи? Действие необратимо.')) return;
      try {
        var res = await fetch('/api/admin/users/' + id, {
          method: 'DELETE',
          headers: { 'X-CSRF-Token': csrf }
        });
        var out = {};
        try { out = await res.json(); } catch (e) {}
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
        setMsg('user-msg', 'Пользователь удалён', true);
        loadUsers();
      } catch (err) { setMsg('user-msg', err.message, false); }
    }

    var userForm = document.getElementById('user-form');
    if (userForm) {
      userForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        var f = e.target;
        try {
          await postJSON('/api/admin/users', {
            username: f.username.value,
            display_name: f.display_name.value,
            password: f.password.value
          });
          setMsg('user-msg', 'Пользователь добавлен', true);
          f.username.value = '';
          f.display_name.value = '';
          f.password.value = '';
          loadUsers();
        } catch (err) { setMsg('user-msg', err.message, false); }
      });
      loadUsers();
    }

    var userEditForm = document.getElementById('user-edit-form');
    if (userEditForm) {
      userEditForm.addEventListener('submit', async function(e) {
        e.preventDefault();
        if (!userEditId) return;
        var f = e.target;
        var payload = {
          username: f.username.value,
          display_name: f.display_name.value
        };
        if (f.password.value) { payload.password = f.password.value; }
        try {
          await sendJSON('PATCH', '/api/admin/users/' + userEditId, payload);
          setMsg('user-edit-msg', 'Сохранено', true);
          cancelUserEdit();
          loadUsers();
        } catch (err) {
          setMsg('user-edit-msg', err.message, false);
        }
      });
    }

    function b64uToBuf(s) {
      s = s.replace(/-/g, '+').replace(/_/g, '/');
      while (s.length % 4) s += '=';
      var bin = atob(s);
      var buf = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return buf.buffer;
    }
    function bufToB64u(buf) {
      var b = new Uint8Array(buf);
      var s = '';
      for (var i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
      return btoa(s).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
    }

    function biometricName() {
      var ua = navigator.userAgent;
      if (/Android/i.test(ua)) return 'отпечаток пальца';
      var isIOS = /iPhone|iPad|iPod/i.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
      if (isIOS) {
        var w = Math.min(screen.width, screen.height);
        var h = Math.max(screen.width, screen.height);
        if ((w === 320 && h === 568) || (w === 375 && h === 667)) return 'Touch ID';
        return 'Face ID';
      }
      return 'биометрию (Windows Hello / Touch ID)';
    }

    (function() {
      var sum = document.getElementById('wa-summary');
      var btn = document.getElementById('wa-enable-btn');
      var avail = document.getElementById('wa-availability');
      var n = biometricName();
      if (sum) { sum.textContent = '🔐 ' + n; }
      if (btn) { btn.textContent = 'Включить ' + n; }
      function noBio() {
        if (avail) { avail.textContent = 'На этом устройстве нет биометрии — вход только по паролю.'; }
        if (btn) { btn.disabled = true; }
      }
      if (!window.PublicKeyCredential || !PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable) { noBio(); return; }
      PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().then(function(av) { if (!av) { noBio(); } });
    })();

    async function waRegister() {
      try {
        if (!window.PublicKeyCredential) throw new Error('WebAuthn не поддерживается на этом устройстве/браузере');
        var res = await fetch('/api/webauthn/register/options', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
        var opts = await res.json();
        if (!res.ok) throw new Error(opts.error || 'HTTP ' + res.status);
        opts.challenge = b64uToBuf(opts.challenge);
        opts.user.id = b64uToBuf(opts.user.id);
        opts.excludeCredentials = (opts.excludeCredentials || []).map(function(c) { c.id = b64uToBuf(c.id); return c; });
        var cred = await navigator.credentials.create({ publicKey: opts });
        var res2 = await fetch('/api/webauthn/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({
            id: cred.id,
            rawId: bufToB64u(cred.rawId),
            type: cred.type,
            response: {
              attestationObject: bufToB64u(cred.response.attestationObject),
              clientDataJSON: bufToB64u(cred.response.clientDataJSON)
            }
          })
        });
        var out = await res2.json();
        if (!res2.ok) throw new Error(out.error || 'HTTP ' + res2.status);
        try { localStorage.setItem('medical_diary_wa_credential_id', cred.id); } catch (e) {}
        setMsg('wa-msg', biometricName() + ' включён для этого устройства', true);
      } catch (err) {
        setMsg('wa-msg', err.message, false);
      }
    }

    async function waDelete() {
      if (!confirm('Отключить вход по Face ID на этом устройстве?')) return;
      try {
        await sendJSON('DELETE', '/api/webauthn/credentials', {});
        try { localStorage.removeItem('medical_diary_wa_credential_id'); } catch (e) {}
        setMsg('wa-msg', 'Вход по биометрии отключён', true);
      } catch (err) {
        setMsg('wa-msg', err.message, false);
      }
    }

    var UNIT_RU_JS = { 'g': 'г', 'ml': 'мл', 'pcs': 'шт', 'portion': 'порция' };
    var editState = { type: null, id: null };

    async function sendJSON(method, url, data) {
      var res = await fetch(url, {
        method: method,
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify(data)
      });

      if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }

      var out = {};
      try { out = await res.json(); } catch (e) {}

      if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
      return out;
    }

    function startEdit(entry) {
      editState.type = entry.type;
      editState.id = entry.id;

      document.getElementById('edit-card').hidden = false;
      document.getElementById('edit-glucose').hidden = entry.type !== 'glucose';
      document.getElementById('edit-vitals').hidden = entry.type !== 'vitals';
      document.getElementById('edit-food').hidden = entry.type !== 'food';
      document.getElementById('edit-title').textContent = '✏️ ' + entry.type_label + ' · ' + entry.measured_at;

      document.getElementById('edit_measured_at').value = entry.measured_at.replace(' ', 'T').substring(0, 16);
      document.getElementById('edit_comment').value = entry.comment || '';

      if (entry.type === 'glucose') {
        document.getElementById('edit_glucose_type').value = entry.glucose_type;
        document.getElementById('edit_glucose_value').value = entry.value_mmol_l;
      } else if (entry.type === 'vitals') {
        document.getElementById('edit_systolic').value = entry.systolic_mmhg;
        document.getElementById('edit_diastolic').value = entry.diastolic_mmhg;
        document.getElementById('edit_pulse').value = (entry.pulse_bpm == null) ? '' : entry.pulse_bpm;
      } else if (entry.type === 'food') {
        document.getElementById('edit_food_name').value = entry.food_name;
        document.getElementById('edit_amount_value').value = entry.amount_value;
        document.getElementById('edit_amount_unit').value = UNIT_RU_JS[entry.amount_unit] || entry.amount_unit;
      }

      setMsg('edit-msg', '', true);
      document.getElementById('edit-card').scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function closeEdit() {
      editState.type = null;
      editState.id = null;
      document.getElementById('edit-card').hidden = true;
    }

    async function saveEdit() {
      if (!editState.id) return;

      var url = '';
      var payload = {};
      var ma = document.getElementById('edit_measured_at').value.replace('T', ' ');
      var cm = document.getElementById('edit_comment').value;

      if (editState.type === 'glucose') {
        url = '/api/glucose/' + editState.id;
        payload = {
          glucose_type: document.getElementById('edit_glucose_type').value,
          value: document.getElementById('edit_glucose_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'vitals') {
        url = '/api/vitals/' + editState.id;
        payload = {
          systolic: document.getElementById('edit_systolic').value,
          diastolic: document.getElementById('edit_diastolic').value,
          pulse: document.getElementById('edit_pulse').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'food') {
        url = '/api/food/' + editState.id;
        payload = {
          food_name: document.getElementById('edit_food_name').value,
          amount_value: document.getElementById('edit_amount_value').value,
          amount_unit: document.getElementById('edit_amount_unit').value,
          consumed_at: ma,
          comment: cm
        };
      }

      try {
        await sendJSON('PATCH', url, payload);
        setMsg('edit-msg', 'Сохранено', true);
        closeEdit();
        loadHistory();
      } catch (err) {
        setMsg('edit-msg', err.message, false);
      }
    }

    async function deleteEntry(entry) {
      if (!confirm('Удалить запись «' + entry.type_label + '» от ' + entry.measured_at + '? Она скроется из истории и PDF.')) return;

      var url = '';
      if (entry.type === 'glucose') { url = '/api/glucose/' + entry.id; }
      else if (entry.type === 'vitals') { url = '/api/vitals/' + entry.id; }
      else if (entry.type === 'food') { url = '/api/food/' + entry.id; }

      try {
        await sendJSON('DELETE', url, {});
        loadHistory();
      } catch (err) {
        setMsg('history-msg', err.message, false);
      }
    }

    var currentExportUrl = '';
    var pdfBlob = null;
    var pdfDoc = null;
    var pdfPages = [];
    var currentZoom = 1;
    var renderedZoom = 1;
    var pinchState = null;
    var pinchBound = false;
    var rerenderTimer = null;
    if (typeof pdfjsLib !== 'undefined') {
      pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.js';
    }

    function clampZoom(z) { return Math.min(4, Math.max(1, z)); }

    function updateZoomLabel() {
      var el = document.getElementById('zoom-label');
      if (el) { el.textContent = Math.round(currentZoom * 100) + '%'; }
    }

    function applyCssZoom(z) {
      var k = z / renderedZoom;
      for (var i = 0; i < pdfPages.length; i++) {
        var c = pdfPages[i].canvas;
        c.style.width = Math.floor(pdfPages[i].cssW * k) + 'px';
        c.style.height = Math.floor(pdfPages[i].cssH * k) + 'px';
      }
    }

    async function renderPdfPages(zoom) {
      var pagesEl = document.getElementById('pdf-pages');
      var containerWidth = Math.max(pagesEl.clientWidth - 20, 200);
      var dprCap = Math.min(window.devicePixelRatio || 1, 2);

      for (var i = 1; i <= pdfDoc.numPages; i++) {
        var page = await pdfDoc.getPage(i);
        var base = page.getViewport({ scale: 1 });
        var scale = (containerWidth / base.width) * zoom;
        var viewport = page.getViewport({ scale: scale });

        var item = pdfPages[i - 1];
        var canvas = item ? item.canvas : document.createElement('canvas');
        canvas.width = Math.floor(viewport.width * dprCap);
        canvas.height = Math.floor(viewport.height * dprCap);
        var cssW = Math.floor(viewport.width);
        var cssH = Math.floor(viewport.height);
        canvas.style.width = cssW + 'px';
        canvas.style.height = cssH + 'px';
        if (!canvas.parentNode) { pagesEl.appendChild(canvas); }

        await page.render({
          canvasContext: canvas.getContext('2d'),
          viewport: viewport,
          transform: dprCap !== 1 ? [dprCap, 0, 0, dprCap, 0, 0] : null
        }).promise;

        pdfPages[i - 1] = { canvas: canvas, cssW: cssW, cssH: cssH };
      }
      renderedZoom = zoom;
    }

    function scheduleRerender() {
      clearTimeout(rerenderTimer);
      rerenderTimer = setTimeout(async function() {
        if (pdfDoc) { await renderPdfPages(currentZoom); }
      }, 250);
    }

    function zoomPdf(dir) {
      currentZoom = clampZoom(currentZoom * (dir > 0 ? 1.25 : 0.8));
      applyCssZoom(currentZoom);
      updateZoomLabel();
      scheduleRerender();
    }

    function pinchDist(e) {
      var dx = e.touches[0].clientX - e.touches[1].clientX;
      var dy = e.touches[0].clientY - e.touches[1].clientY;
      return Math.sqrt(dx * dx + dy * dy);
    }

    function bindPinch() {
      if (pinchBound) return;
      pinchBound = true;
      var pagesEl = document.getElementById('pdf-pages');

      pagesEl.addEventListener('touchstart', function(e) {
        if (e.touches.length === 2) {
          pinchState = { d: pinchDist(e), z: currentZoom };
          e.preventDefault();
        }
      }, { passive: false });

      pagesEl.addEventListener('touchmove', function(e) {
        if (pinchState && e.touches.length === 2) {
          e.preventDefault();
          currentZoom = clampZoom(pinchState.z * pinchDist(e) / pinchState.d);
          applyCssZoom(currentZoom);
          updateZoomLabel();
        }
      }, { passive: false });

      pagesEl.addEventListener('touchend', function(e) {
        if (pinchState && e.touches.length < 2) {
          pinchState = null;
          if (Math.abs(currentZoom - renderedZoom) > 0.01) { scheduleRerender(); }
        }
      });
    }

    async function openPdfViewer() {
      if (!currentExportUrl) { setMsg('history-msg', 'Сначала дождитесь загрузки истории', false); return; }
      if (typeof pdfjsLib === 'undefined') { setMsg('history-msg', 'pdf.js не загрузился', false); return; }

      var overlay = document.getElementById('pdf-overlay');
      var pages = document.getElementById('pdf-pages');
      overlay.hidden = false;
      document.body.style.overflow = 'hidden';
      pages.innerHTML = '<div class="pdf-status">⏳ Формирование PDF…</div>';

      currentZoom = 1;
      renderedZoom = 1;
      pdfPages = [];
      updateZoomLabel();

      try {
        var res = await fetch(currentExportUrl);
        if (res.status === 401) { window.location = '/login'; return; }
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        pdfBlob = await res.blob();
        var data = await pdfBlob.arrayBuffer();
        pdfDoc = await pdfjsLib.getDocument({ data: data }).promise;
        pages.innerHTML = '';
        pdfPages = [];
        await renderPdfPages(1);
        bindPinch();
      } catch (err) {
        pages.innerHTML = '<div class="pdf-status">Ошибка просмотра: ' + err.message + '</div>';
      }
    }

    async function sharePdf() {
      if (!pdfBlob) return;
      try {
        var file = new File([pdfBlob], 'medical_diary.pdf', { type: 'application/pdf' });
        if (navigator.canShare && navigator.canShare({ files: [file] })) {
          await navigator.share({ files: [file], title: 'Медицинский дневник' });
        } else {
          var a = document.createElement('a');
          a.href = URL.createObjectURL(pdfBlob);
          a.download = 'medical_diary.pdf';
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function() { URL.revokeObjectURL(a.href); }, 5000);
        }
      } catch (err) {
        if (err && err.name !== 'AbortError') { alert('Не удалось поделиться: ' + err.message); }
      }
    }

    function closePdfViewer() {
      document.getElementById('pdf-overlay').hidden = true;
      document.body.style.overflow = '';
      if (pdfDoc) { pdfDoc.destroy(); pdfDoc = null; }
      pdfBlob = null;
      pdfPages = [];
      currentZoom = 1;
      renderedZoom = 1;
      document.getElementById('pdf-pages').innerHTML = '';
    }

    async function logout() {
      try {
        await fetch('/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } });
      } catch (e) {}
      window.location = '/login';
    }

    loadHistory();
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
