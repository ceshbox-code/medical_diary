import os
import sqlite3
import secrets
import json
import threading
import glob
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

from flask import (
    Flask,
    request,
    jsonify,
    session,
    redirect,
    url_for,
    render_template,
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

from validators import (
    UNIT_RU,
    unit_ru,
    DEFAULT_RANGES,
    MONTHS_RU_SHORT,
    now_local,
    parse_dt,
    parse_iso_date,
    parse_float,
    parse_int,
    format_dt_ru,
)
from ai_utils import (
    _ai_ranges_payload,
    _ai_input_hash,
    _ai_blocked_by_safety_override,
    compute_period_stats,
)
from db import DATABASE, SCHEMA, get_db, close_db, init_db
from reminders import reminders_bp
from push import push_bp, start_push_scheduler
from mdlp_watcher import start_mdlp_watcher
from security import (
    audit,
    LOGIN_MAX_ATTEMPTS,
    LOGIN_LOCK_MINUTES,
    throttle_key_for,
    is_login_locked,
    register_login_failure,
    clear_login_failures,
    get_idempotent_response,
    store_idempotent_response,
    wants_json_response,
    login_required,
    admin_required,
    mark_session_active,
    csrf_protect,
    security_headers,
)
from assessments import (
    add_assessments,
    add_ai_assessments,
    add_ai_dynamics_summary,
    GIGACHAT_AI_ENABLED,
    GIGACHAT_AUTH_KEY,
    GIGACHAT_MODEL,
    GIGACHAT_API_URL,
    GIGACHAT_TIMEOUT_SECONDS,
    _gigachat_get_access_token,
    _gigachat_invalidate_token,
    _normalize_ai_text,
)
from pdf_export import (
    VALID_ENTRY_TYPES,
    _parse_entry_types,
    query_entries,
    build_pdf,
)
from backup import (
    BACKUP_ENABLED,
    BACKUP_DIR,
    BACKUP_HOUR,
    BACKUP_MINUTE,
    BACKUP_RETENTION_DAYS,
    backup_database,
    restore_database_from_backup,
    backup_scheduler_loop,
)

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


app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_NAME="medical_diary_session",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
app.permanent_session_lifetime = timedelta(days=int(os.getenv("SESSION_LIFETIME_DAYS", "30")))

# csrf_protect/security_headers/close_db теперь определены в security.py/db.py
# (не создают объект Flask-приложения сами), поэтому регистрируются здесь.
app.teardown_appcontext(close_db)
app.before_request(csrf_protect)
app.after_request(security_headers)
app.register_blueprint(reminders_bp)
app.register_blueprint(push_bp)


DEFAULT_SETTINGS = {"glucose": True, "vitals": True, "food": True, "temperature": True, "weight": True, "ranges_default": True, "ai_enabled": True}

# Допустимые абсолютные границы персональных диапазонов по ключам. Для ключей
# без явной записи действует прежний общий предел 0–1000.
RANGE_ABS_LIMITS = {"temperature": (30.0, 45.0)}
RANGE_ABS_LIMIT_DEFAULT = (0.0, 1000.0)


def _range_limits(key):
    return RANGE_ABS_LIMITS.get(key, RANGE_ABS_LIMIT_DEFAULT)


def get_user_settings(user_id):
    """Настройки пользователя (видимость блоков + персональные диапазоны),
    смёрженные с значениями по умолчанию. Некорректные/битые сохранённые
    данные тихо игнорируются — пользователь просто получает диапазоны по
    умолчанию, а не ошибку 500."""
    settings = dict(DEFAULT_SETTINGS)
    ranges = {k: tuple(v) for k, v in DEFAULT_RANGES.items()}
    db = get_db()
    row = db.execute("SELECT settings_json FROM users WHERE id = ?", (user_id,)).fetchone()
    if row and row["settings_json"]:
        try:
            stored = json.loads(row["settings_json"])
            for key in DEFAULT_SETTINGS:
                if key in stored:
                    settings[key] = bool(stored[key])
            # Пока включён режим "по умолчанию", сохранённые персональные
            # диапазоны игнорируются (но НЕ стираются) — так пользователь
            # может временно вернуться к дефолтным значениям и затем снова
            # включить свои старые числа, не вводя их заново.
            if not settings.get("ranges_default", True):
                custom_ranges = stored.get("ranges") or {}
                for key, bounds in custom_ranges.items():
                    if key not in ranges or not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                        continue
                    try:
                        low, high = float(bounds[0]), float(bounds[1])
                    except (TypeError, ValueError):
                        continue
                    lim_lo, lim_hi = _range_limits(key)
                    if lim_lo <= low < high <= lim_hi:
                        ranges[key] = (low, high)
        except Exception:
            pass
    return settings, ranges


init_db()


if BACKUP_ENABLED:
    print(
        f"[backup] Автобэкап включён: каталог {BACKUP_DIR}, "
        f"время {BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d} (по времени сервера), "
        f"хранение {BACKUP_RETENTION_DAYS} дн.",
        flush=True,
    )
    threading.Thread(target=backup_scheduler_loop, daemon=True).start()
else:
    print("[backup] Автобэкап отключён (BACKUP_ENABLED=false)", flush=True)

# Планировщик уведомлений (Web Push). Приложение запускается одним воркером
# gunicorn (--workers 1), поэтому поток запускается один раз; повторную
# отправку всё равно исключает журнал notification_deliveries.
start_push_scheduler(app)

# Наблюдатель за каталогом с еженедельными выгрузками ЦРПТ (GTIN -> лекарство,
# см. mdlp_watcher.py) — просто положить новый файл в /data/mdlp_import/,
# импорт запустится сам при следующей проверке.
start_mdlp_watcher(app)


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
            return render_template("login.html", error=error)

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
            mark_session_active()
            audit("login_success", "user", user["id"], {"username": username})
            return redirect(url_for("dashboard"))

        register_login_failure(tkey)
        audit("login_failed", "user", None, {"username": username})
        error = "Неверный логин или пароль"

    return render_template("login.html", error=error)


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

    settings, ranges = get_user_settings(session["user_id"])
    return render_template(
        "app.html",
        csrf_token=session["csrf_token"],
        display_name=session.get("display_name") or session.get("username", ""),
        is_admin=1 if session.get("is_admin") else 0,
        user_id=session.get("user_id", 0),
        settings_json=json.dumps(settings),
        ranges_json=json.dumps({k: list(v) for k, v in ranges.items()}),
        default_ranges_json=json.dumps({k: list(v) for k, v in DEFAULT_RANGES.items()}),
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


@app.post("/api/temperature")
@login_required
def api_temperature():
    data = request.get_json(silent=True) or {}
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    cached = get_idempotent_response(session["user_id"], "api_temperature", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        value = parse_float(data.get("value"), 0.1, 100.0, "Температура")
        measured_at = parse_dt(data.get("measured_at"))
        comment = str(data.get("comment") or "").strip()[:1000]

        db = get_db()
        cur = db.execute(
            "INSERT INTO temperature_entries (user_id, measured_at, temperature_c, comment, source) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], measured_at, value, comment, "manual"),
        )
        db.commit()

        audit("create_temperature", "temperature_entries", cur.lastrowid)
        result = {"ok": True, "id": cur.lastrowid}
        store_idempotent_response(session["user_id"], "api_temperature", idem_key, "temperature_entries", cur.lastrowid, result)
        return jsonify(result)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/weight")
@login_required
def api_weight():
    data = request.get_json(silent=True) or {}
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    cached = get_idempotent_response(session["user_id"], "api_weight", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        value = parse_float(data.get("value"), 1.0, 500.0, "Вес")
        measured_at = parse_dt(data.get("measured_at"))
        comment = str(data.get("comment") or "").strip()[:1000]

        db = get_db()
        cur = db.execute(
            "INSERT INTO weight_entries (user_id, measured_at, weight_kg, comment, source) VALUES (?, ?, ?, ?, ?)",
            (session["user_id"], measured_at, value, comment, "manual"),
        )
        db.commit()

        audit("create_weight", "weight_entries", cur.lastrowid)
        result = {"ok": True, "id": cur.lastrowid}
        store_idempotent_response(session["user_id"], "api_weight", idem_key, "weight_entries", cur.lastrowid, result)
        return jsonify(result)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/last")
@login_required
def api_last_entry():
    entry_type = request.args.get("type", "")
    if entry_type not in ("glucose", "vitals", "food", "temperature", "weight"):
        return jsonify(error="Некорректный тип"), 400

    db = get_db()
    if entry_type == "glucose":
        r = db.execute(
            "SELECT * FROM glucose_entries WHERE user_id = ? AND deleted_at IS NULL ORDER BY measured_at DESC LIMIT 1",
            (session["user_id"],),
        ).fetchone()
        if not r:
            return jsonify(found=False)
        return jsonify(
            found=True,
            glucose_type=r["glucose_type"],
            value=float(r["value_mmol_l"]),
            comment=r["comment"] or "",
            measured_at=r["measured_at"],
            measured_at_ru=format_dt_ru(r["measured_at"]),
        )
    if entry_type == "vitals":
        r = db.execute(
            "SELECT * FROM blood_pressure_entries WHERE user_id = ? AND deleted_at IS NULL ORDER BY measured_at DESC LIMIT 1",
            (session["user_id"],),
        ).fetchone()
        if not r:
            return jsonify(found=False)
        return jsonify(
            found=True,
            systolic=r["systolic_mmhg"],
            diastolic=r["diastolic_mmhg"],
            pulse=r["pulse_bpm"],
            comment=r["comment"] or "",
            measured_at=r["measured_at"],
            measured_at_ru=format_dt_ru(r["measured_at"]),
        )
    if entry_type == "temperature":
        r = db.execute(
            "SELECT * FROM temperature_entries WHERE user_id = ? AND deleted_at IS NULL ORDER BY measured_at DESC LIMIT 1",
            (session["user_id"],),
        ).fetchone()
        if not r:
            return jsonify(found=False)
        return jsonify(
            found=True,
            value=float(r["temperature_c"]),
            comment=r["comment"] or "",
            measured_at=r["measured_at"],
            measured_at_ru=format_dt_ru(r["measured_at"]),
        )
    if entry_type == "weight":
        r = db.execute(
            "SELECT * FROM weight_entries WHERE user_id = ? AND deleted_at IS NULL ORDER BY measured_at DESC LIMIT 1",
            (session["user_id"],),
        ).fetchone()
        if not r:
            return jsonify(found=False)
        return jsonify(
            found=True,
            value=float(r["weight_kg"]),
            comment=r["comment"] or "",
            measured_at=r["measured_at"],
            measured_at_ru=format_dt_ru(r["measured_at"]),
        )
    r = db.execute(
        "SELECT * FROM food_entries WHERE user_id = ? AND deleted_at IS NULL ORDER BY consumed_at DESC LIMIT 1",
        (session["user_id"],),
    ).fetchone()
    if not r:
        return jsonify(found=False)
    return jsonify(
        found=True,
        food_name=r["food_name"],
        amount_value=float(r["amount_value"]),
        amount_unit=unit_ru(r["amount_unit"]),
        comment=r["comment"] or "",
        measured_at=r["consumed_at"],
        measured_at_ru=format_dt_ru(r["consumed_at"]),
    )


@app.get("/api/history")
@login_required
def api_history():
    try:
        _settings, ranges = get_user_settings(session["user_id"])
        entries, d_from, d_to = query_entries(
            session["user_id"],
            request.args.get("date_from"),
            request.args.get("date_to"),
            request.args.get("type", "all"),
            request.args.get("sort", "date"),
            ranges,
        )
        entries = add_assessments(entries, ranges)
        return jsonify(entries=entries, date_from=d_from, date_to=d_to)
    except ValueError as e:
        return jsonify(error=str(e)), 400


@app.get("/api/history/ai-dynamics")
@login_required
def api_history_ai_dynamics():
    """Оценка динамики показателей за выбранный на вкладке «История»
    период (тот же период/тип, что и в текущих фильтрах — /api/history).

    Числовая статистика (min/avg/max/дельта/распределение по диапазону)
    считается детерминированно в Python (compute_period_stats) — это
    факты и расчёты. GigaChat получает ТОЛЬКО эти уже готовые числа и
    формулирует по ним нейтральный текст (assessments.add_ai_dynamics_summary) —
    это отдельный, явно помеченный вывод, не диагноз и не назначение.
    """
    _settings, ranges = get_user_settings(session["user_id"])

    try:
        entries, d_from, d_to = query_entries(
            session["user_id"],
            request.args.get("date_from"),
            request.args.get("date_to"),
            request.args.get("type", "all"),
            "date",
            ranges,
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400

    if len(entries) < 2:
        return jsonify(error="Недостаточно записей за выбранный период для анализа динамики (нужно минимум 2)."), 400

    entries = add_assessments(entries, ranges)
    stats = compute_period_stats(entries)
    if not stats:
        return jsonify(error="Нет числовых показателей за выбранный период для анализа динамики."), 400

    ranges_payload = _ai_ranges_payload(ranges)
    result = add_ai_dynamics_summary(
        stats, ranges_payload, d_from, d_to,
        request.args.get("type", "all"),
        enabled=_settings.get("ai_enabled", True),
    )

    if not result["ok"]:
        error_responses = {
            "disabled": (503, "ИИ-оценка динамики недоступна (отключена в настройках или не настроена на сервере)."),
            "throttled": (429, "Слишком частые запросы к ИИ. Подождите немного и повторите."),
            "gigachat_failed": (502, "Не удалось получить оценку динамики от ИИ. Попробуйте позже."),
        }
        status_code, message = error_responses.get(result.get("error"), (502, "Не удалось получить оценку динамики от ИИ. Попробуйте позже."))
        return jsonify(error=message), status_code

    audit(
        "ai_dynamics_summary",
        "history",
        None,
        {
            "date_from": d_from,
            "date_to": d_to,
            "type": request.args.get("type", "all"),
            "cache": "hit" if result["cached"] else "miss",
        },
    )

    return jsonify(
        summary=result["summary"],
        observations=result["observations"],
        caution=result["caution"],
        stats=stats,
        date_from=d_from,
        date_to=d_to,
        cached=result["cached"],
    )


@app.get("/export.pdf")
@login_required
def export_pdf():
    _settings, ranges = get_user_settings(session["user_id"])
    try:
        entries, d_from, d_to = query_entries(
            session["user_id"],
            request.args.get("date_from"),
            request.args.get("date_to"),
            request.args.get("type", "all"),
            request.args.get("sort", "date"),
            ranges,
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400

    entries = add_assessments(entries, ranges)
    entries, ai_used = add_ai_assessments(entries, ranges, enabled=_settings.get("ai_enabled", True))

    type_short_labels = {
        "glucose": "глюкоза",
        "vitals": "давление и пульс",
        "temperature": "температура",
        "weight": "вес",
        "food": "питание",
    }
    requested_type = request.args.get("type", "all")
    try:
        requested_set = _parse_entry_types(requested_type)
    except ValueError:
        requested_set = set(VALID_ENTRY_TYPES)

    if requested_set == set(VALID_ENTRY_TYPES):
        type_label = "Все записи"
    else:
        ordered = [t for t in VALID_ENTRY_TYPES if t in requested_set]
        if len(ordered) == 1:
            type_label = f"Только {type_short_labels[ordered[0]]}"
        else:
            labels = [type_short_labels[t] for t in ordered]
            labels[0] = labels[0][0].upper() + labels[0][1:]
            type_label = ", ".join(labels)

    db = get_db()
    owner = db.execute(
        "SELECT display_name, username FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    owner_name = (owner["display_name"] or owner["username"]) if owner else ""

    buffer = build_pdf(entries, d_from, d_to, request.args.get("sort", "date"), type_label, owner_name, ai_used)

    audit("export_pdf", None, None, {"date_from": d_from, "date_to": d_to, "ai_assessment": bool(ai_used), "ai_model": GIGACHAT_MODEL if ai_used else None})

    filename = f"medical_diary_{d_from}_{d_to}.pdf"

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@app.get("/api/admin/backups")
@admin_required
def admin_list_backups():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    items = []
    for path in sorted(glob.glob(os.path.join(BACKUP_DIR, "medical_diary-*.db")), reverse=True):
        try:
            stat = os.stat(path)
            items.append(
                {
                    "name": os.path.basename(path),
                    "size_bytes": stat.st_size,
                    "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except OSError:
            continue
    return jsonify(
        enabled=BACKUP_ENABLED,
        backup_dir=BACKUP_DIR,
        scheduled_time=f"{BACKUP_HOUR:02d}:{BACKUP_MINUTE:02d}",
        retention_days=BACKUP_RETENTION_DAYS,
        backups=items,
    )


@app.post("/api/admin/backups/run")
@admin_required
def admin_run_backup():
    path = backup_database(force=True)
    if not path:
        return jsonify(error="Не удалось создать резервную копию — подробности в логах сервера"), 500
    audit("manual_backup", "backups", None, {"file": os.path.basename(path)})
    return jsonify(ok=True, file=os.path.basename(path))


@app.post("/api/admin/backups/restore")
@admin_required
def admin_restore_backup():
    data = request.get_json(silent=True) or {}
    filename = str(data.get("filename") or "").strip()

    # Принимаем только имя файла, а не произвольный путь.
    if not filename or os.path.basename(filename) != filename:
        return jsonify(error="Некорректное имя резервной копии"), 400
    if not filename.startswith("medical_diary-") or not filename.endswith(".db"):
        return jsonify(error="Некорректное имя резервной копии"), 400

    backup_path = os.path.join(BACKUP_DIR, filename)
    backup_real = os.path.realpath(backup_path)
    backup_dir_real = os.path.realpath(BACKUP_DIR)
    if os.path.commonpath([backup_real, backup_dir_real]) != backup_dir_real:
        return jsonify(error="Недопустимый путь к резервной копии"), 400

    db = get_db()
    admin_row = db.execute(
        "SELECT username FROM users WHERE id = ? AND status = 'active' AND is_admin = 1",
        (session["user_id"],),
    ).fetchone()
    if not admin_row:
        session.clear()
        return jsonify(error="Учётная запись администратора недоступна"), 401

    try:
        emergency_path = restore_database_from_backup(
            backup_real,
            admin_row["username"],
        )
        # Аудит записываем уже в восстановленную БД, поэтому событие
        # остаётся вместе с восстановленными данными.
        audit(
            "restore_database",
            "backups",
            None,
            {
                "file": filename,
                "emergency_backup": os.path.basename(emergency_path),
            },
        )
        session.clear()
        return jsonify(
            ok=True,
            restored_file=filename,
            emergency_backup=os.path.basename(emergency_path),
            message="База восстановлена. Для гарантированного перехода всех процессов на новую БД перезапустите приложение.",
        )
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as e:
        return jsonify(error=str(e)), 400


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


@app.patch("/api/temperature/<int:entry_id>")
@login_required
def api_temperature_update(entry_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute(
        "SELECT * FROM temperature_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    try:
        value = parse_float(data.get("value", row["temperature_c"]), 0.1, 100.0, "Температура")
        measured_at = parse_dt(data.get("measured_at") or row["measured_at"])
        comment = str(data.get("comment") or "").strip()[:1000]
    except ValueError as e:
        return jsonify(error=str(e)), 400

    old = {"temperature_c": row["temperature_c"], "measured_at": row["measured_at"], "comment": row["comment"]}
    new = {"temperature_c": value, "measured_at": measured_at, "comment": comment}

    db.execute(
        "UPDATE temperature_entries SET temperature_c = ?, measured_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (value, measured_at, comment, entry_id),
    )
    db.commit()
    audit("update_temperature", "temperature_entries", entry_id, {"old": old, "new": new})
    return jsonify(ok=True)


@app.delete("/api/temperature/<int:entry_id>")
@login_required
def api_temperature_delete(entry_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM temperature_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    db.execute(
        "UPDATE temperature_entries SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (entry_id,),
    )
    db.commit()
    audit("delete_temperature", "temperature_entries", entry_id, {"old": {"temperature_c": row["temperature_c"], "measured_at": row["measured_at"], "comment": row["comment"]}})
    return jsonify(ok=True)


@app.patch("/api/weight/<int:entry_id>")
@login_required
def api_weight_update(entry_id):
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute(
        "SELECT * FROM weight_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    try:
        value = parse_float(data.get("value", row["weight_kg"]), 1.0, 500.0, "Вес")
        measured_at = parse_dt(data.get("measured_at") or row["measured_at"])
        comment = str(data.get("comment") or "").strip()[:1000]
    except ValueError as e:
        return jsonify(error=str(e)), 400

    old = {"weight_kg": row["weight_kg"], "measured_at": row["measured_at"], "comment": row["comment"]}
    new = {"weight_kg": value, "measured_at": measured_at, "comment": comment}

    db.execute(
        "UPDATE weight_entries SET weight_kg = ?, measured_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (value, measured_at, comment, entry_id),
    )
    db.commit()
    audit("update_weight", "weight_entries", entry_id, {"old": old, "new": new})
    return jsonify(ok=True)


@app.delete("/api/weight/<int:entry_id>")
@login_required
def api_weight_delete(entry_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM weight_entries WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (entry_id, session["user_id"]),
    ).fetchone()
    if not row:
        return jsonify(error="Запись не найдена"), 404

    db.execute(
        "UPDATE weight_entries SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (entry_id,),
    )
    db.commit()
    audit("delete_weight", "weight_entries", entry_id, {"old": {"weight_kg": row["weight_kg"], "measured_at": row["measured_at"], "comment": row["comment"]}})
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


@app.get("/api/ai-status")
@login_required
def api_ai_status():
    """Проверяет конфигурацию и OAuth GigaChat без передачи медицинских данных."""
    if not GIGACHAT_AI_ENABLED:
        return jsonify(
            available=False, configured=bool(GIGACHAT_AUTH_KEY), enabled=False,
            model=GIGACHAT_MODEL, code="disabled",
            message="ИИ отключён на сервере",
        )
    if not GIGACHAT_AUTH_KEY:
        return jsonify(
            available=False, configured=False, enabled=True,
            model=GIGACHAT_MODEL, code="missing_key",
            message="Не задан GIGACHAT_AUTH_KEY",
        )
    try:
        token = _gigachat_get_access_token()
        if not token:
            return jsonify(
                available=False, configured=True, enabled=True,
                model=GIGACHAT_MODEL, code="token_missing",
                message="GigaChat не вернул access token",
            )
        return jsonify(
            available=True, configured=True, enabled=True,
            model=GIGACHAT_MODEL, code="ok",
            message="GigaChat доступен",
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            code, message = "auth_error", "Ошибка авторизации GigaChat: проверьте GIGACHAT_AUTH_KEY"
            _gigachat_invalidate_token()
        elif exc.code == 403:
            code, message = "forbidden", "GigaChat отклонил запрос (403)"
        elif exc.code == 429:
            code, message = "rate_limit", "GigaChat временно ограничил частоту запросов"
        else:
            code, message = "http_error", f"GigaChat вернул HTTP {exc.code}"
        print(f"[gigachat] status check failed: HTTP {exc.code}", flush=True)
        return jsonify(
            available=False, configured=True, enabled=True,
            model=GIGACHAT_MODEL, code=code, message=message,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[gigachat] status check failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify(
            available=False, configured=True, enabled=True,
            model=GIGACHAT_MODEL, code="network_error",
            message="GigaChat недоступен по сети",
        )
    except Exception as exc:
        print(f"[gigachat] status check failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify(
            available=False, configured=True, enabled=True,
            model=GIGACHAT_MODEL, code="error",
            message="Ошибка подключения к GigaChat",
        )


@app.post("/api/ai-test")
@login_required
def api_ai_test():
    """Реальный короткий тест генерации без медицинских данных."""
    if not GIGACHAT_AI_ENABLED:
        return jsonify(ok=False, code="disabled", message="ИИ отключён на сервере"), 400
    try:
        token = _gigachat_get_access_token()
        if not token:
            return jsonify(ok=False, code="token_missing", message="Не удалось получить токен GigaChat"), 502

        body = {
            "model": GIGACHAT_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": "Ответь одним словом: ГОТОВО",
                }
            ],
            "temperature": 0.0,
            "max_tokens": 20,
        }
        req = urllib.request.Request(
            GIGACHAT_API_URL,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
                "User-Agent": "MedicalDiary/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=GIGACHAT_TIMEOUT_SECONDS) as response:
            result = json.loads(response.read(64 * 1024).decode("utf-8"))

        answer = _normalize_ai_text(result["choices"][0]["message"]["content"], 80)
        if not answer:
            raise ValueError("GigaChat вернул пустой ответ")
        return jsonify(ok=True, model=result.get("model") or GIGACHAT_MODEL, answer=answer)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            code, message = "auth_error", "Ошибка авторизации GigaChat"
            _gigachat_invalidate_token()
        elif exc.code == 403:
            code, message = "forbidden", "GigaChat отклонил запрос (403)"
        elif exc.code == 402:
            code, message = "payment_required", "Для этого запроса недоступен лимит GigaChat"
        elif exc.code == 429:
            code, message = "rate_limit", "GigaChat временно ограничил частоту запросов"
        else:
            code, message = "http_error", f"GigaChat вернул HTTP {exc.code}"
        print(f"[gigachat] generation test failed: HTTP {exc.code}", flush=True)
        return jsonify(ok=False, code=code, message=message), 502
    except (urllib.error.URLError, TimeoutError, OSError):
        return jsonify(ok=False, code="network_error", message="GigaChat недоступен по сети"), 502
    except Exception as exc:
        print(f"[gigachat] generation test failed: {type(exc).__name__}: {exc}", flush=True)
        return jsonify(ok=False, code="error", message="Ошибка тестового запроса GigaChat"), 502


@app.post("/api/settings")
@login_required
def api_set_settings():
    data = request.get_json(silent=True) or {}
    db = get_db()
    row = db.execute("SELECT settings_json FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    try:
        stored = json.loads(row["settings_json"]) if row and row["settings_json"] else {}
    except Exception:
        stored = {}

    current = dict(DEFAULT_SETTINGS)
    current.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    for key in DEFAULT_SETTINGS:
        if key in data:
            current[key] = bool(data[key])

    current_ranges = {k: list(v) for k, v in DEFAULT_RANGES.items()}
    stored_ranges = stored.get("ranges") or {}
    for key in DEFAULT_RANGES:
        bounds = stored_ranges.get(key)
        if isinstance(bounds, (list, tuple)) and len(bounds) == 2:
            current_ranges[key] = list(bounds)

    if "ranges" in data:
        incoming_ranges = data["ranges"]
        if not isinstance(incoming_ranges, dict):
            return jsonify(error="Некорректный формат диапазонов"), 400
        for key, bounds in incoming_ranges.items():
            if key not in DEFAULT_RANGES:
                continue
            if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
                return jsonify(error=f"Диапазон «{key}» должен быть парой чисел [мин, макс]"), 400
            try:
                low, high = float(bounds[0]), float(bounds[1])
            except (TypeError, ValueError):
                return jsonify(error=f"Диапазон «{key}»: значения должны быть числами"), 400
            lim_lo, lim_hi = _range_limits(key)
            if not (lim_lo <= low < high <= lim_hi):
                return jsonify(error=f"Диапазон «{key}»: минимум должен быть меньше максимума ({lim_lo:g}–{lim_hi:g})"), 400
            current_ranges[key] = [low, high]

    current["ranges"] = current_ranges
    db.execute(
        "UPDATE users SET settings_json = ?, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(current), session["user_id"]),
    )
    db.commit()
    audit(
        "update_settings",
        "users",
        session["user_id"],
        {"settings": {k: v for k, v in current.items() if k in DEFAULT_SETTINGS}, "ranges_changed": "ranges" in data},
    )
    # Если в итоге включён режим "по умолчанию" — отдаём клиенту именно
    # дефолтные диапазоны, а не то, что лежит в базе, чтобы фронтенд не
    # применял чужие (старые персональные) числа, пока переключатель "по
    # умолчанию" включён.
    effective_ranges = (
        {k: list(v) for k, v in DEFAULT_RANGES.items()}
        if current.get("ranges_default", True)
        else current_ranges
    )
    return jsonify(
        ok=True,
        settings={k: v for k, v in current.items() if k in DEFAULT_SETTINGS},
        ranges=effective_ranges,
    )


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


@app.get("/api/webauthn/status")
@login_required
def wa_status():
    # Источник истины для кнопок "Включить/Отключить" в настройках:
    # спрашиваем сервер, а не полагаемся только на localStorage — ключ
    # мог быть зарегистрирован на другом устройстве этого же аккаунта,
    # и "Отключить" (удаляет все ключи аккаунта) должен быть доступен
    # и там, даже если именно на этом устройстве Face ID не включали.
    db = get_db()
    row = db.execute(
        "SELECT COUNT(*) AS c FROM webauthn_credentials WHERE user_id = ?",
        (session["user_id"],),
    ).fetchone()
    return jsonify(registered=bool(row["c"]))


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
    mark_session_active()
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
