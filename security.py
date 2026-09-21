"""Авторизация, CSRF, аудит, защита от подбора пароля, идемпотентность.

Извлечено из app.py на шаге 4 модуляризации.

csrf_protect и security_headers регистрируются в app.py как
`app.before_request(csrf_protect)` / `app.after_request(security_headers)` —
этот модуль не создаёт объект Flask-приложения и не должен на него
ссылаться напрямую (используется `flask.current_app` там, где нужен
доступ к конфигурации приложения — это и есть стандартный способ Flask
для кода вне главного модуля избежать циклического импорта).
"""

import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import session, request, jsonify, redirect, url_for, abort, current_app

from db import get_db
from validators import now_local


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


# --- Блокировка по неактивности для аккаунтов с включённым Face ID ---------
# Если у пользователя есть хотя бы один зарегистрированный passkey
# (настройка «Вход по Face ID»), сессия считается «разблокированной» только
# пока пользователь активен. После BIOMETRIC_LOCK_MINUTES без запросов
# сессия сбрасывается, и приложение открывает /login, где автоматически
# запускается Face ID (пароль остаётся запасным вариантом).
# 0 (или отрицательное значение) полностью отключает блокировку.
def _read_lock_minutes():
    try:
        return int(os.getenv("BIOMETRIC_LOCK_MINUTES", "5"))
    except ValueError:
        return 5


BIOMETRIC_LOCK_MINUTES = _read_lock_minutes()


def mark_session_active():
    """Фиксирует момент последней активности в сессии (unix-время, сервер)."""
    session["last_activity"] = int(time.time())


def biometric_lock_expired(user_id):
    """True, если у пользователя включён Face ID и сессия простаивала дольше
    допустимого. Отсутствие/некорректное значение last_activity (например,
    сессия создана до появления этой проверки) трактуется как «просрочено» —
    безопасный вариант: один раз потребуется повторный вход."""
    if BIOMETRIC_LOCK_MINUTES <= 0:
        return False
    db = get_db()
    has_key = db.execute(
        "SELECT 1 FROM webauthn_credentials WHERE user_id = ? LIMIT 1", (user_id,)
    ).fetchone()
    if not has_key:
        return False
    try:
        idle = time.time() - int(session.get("last_activity"))
    except (TypeError, ValueError):
        return True
    # idle < 0 (часы сервера откатились) тоже считаем просроченным.
    return idle < 0 or idle > BIOMETRIC_LOCK_MINUTES * 60


def _lock_session_response(user_id):
    audit("biometric_lock", "user", user_id, {"idle_limit_min": BIOMETRIC_LOCK_MINUTES})
    session.clear()
    if wants_json_response():
        return jsonify(error="Требуется повторный вход", code="locked"), 401
    return redirect(url_for("login"))


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

        if biometric_lock_expired(session["user_id"]):
            return _lock_session_response(session["user_id"])
        mark_session_active()

        return f(*args, **kwargs)

    return wrapper


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
        if biometric_lock_expired(session["user_id"]):
            return _lock_session_response(session["user_id"])
        mark_session_active()
        return f(*args, **kwargs)

    return wrapper


def csrf_protect():
    if request.method in ("POST", "DELETE", "PUT", "PATCH") and request.path not in ("/login", "/api/webauthn/login/options", "/api/webauthn/login"):
        token = request.headers.get("X-CSRF-Token")

        if not token and request.is_json:
            data = request.get_json(silent=True) or {}
            token = data.get("csrf_token")

        session_token = session.get("csrf_token")
        if not session_token or not token or not secrets.compare_digest(str(token), str(session_token)):
            abort(403)


def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    # HTML-страницы (дашборд, вход) не должны браться из HTTP-кэша браузера:
    # иначе блокировка по неактивности обходилась бы показом сохранённой копии.
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store"
    if current_app.config.get("SESSION_COOKIE_SECURE"):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:;"
    )
    return response
