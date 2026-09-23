"""Web Push: подписки устройств, VAPID-ключи и планировщик уведомлений (шаг 3).

Что делает модуль:
  * хранит подписки устройств (push_subscriptions) и отдаёт браузеру
    публичный VAPID-ключ;
  * раз в PUSH_TICK_SECONDS проверяет, наступило ли время приёма лекарства
    или напоминания об измерении, и отправляет уведомление на устройства
    пользователя;
  * гарантирует «не более одного раза»: каждое событие сначала «занимается»
    строкой в notification_deliveries (UNIQUE), и только потом отправляется.

Принципы:
  * Текст уведомления нейтральный: без названий лекарств, доз и значений
    измерений — экран блокировки видят посторонние. Исключение — заголовок
    «своего» напоминания, который пользователь написал сам.
  * Приём «не отмечен» ≠ «не принят»: повторное уведомление просто
    напоминает открыть дневник, никаких выводов о здоровье не делается.
  * Если pywebpush не установлен или ключ не читается, приложение работает
    как раньше, а /api/push/config сообщает причину (available = false).
  * Время — локальное время сервера (TZ контейнера), как во всём приложении.

Настройки (переменные окружения, все необязательные):
  PUSH_ENABLED (true), PUSH_TICK_SECONDS (30), PUSH_LOOKBACK_MIN (5),
  PUSH_REPEAT_MIN (15; 0 — без повтора), PUSH_SUPPRESS_BEFORE_MIN (60),
  PUSH_MAX_ATTEMPTS (3), VAPID_SUBJECT (https://<WA_RP_ID>),
  VAPID_KEY_FILE (<каталог БД>/vapid_private.pem),
  PUSH_ALLOWED_HOSTS (дополнительные хосты push-сервисов через запятую;
  запись, начинающаяся с точки, — суффикс).
"""

import base64
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

from flask import Blueprint, jsonify, request, session

import db as _db
from db import get_db
from security import audit, login_required

try:  # библиотека необязательна: без неё уведомления просто «недоступны»
    from pywebpush import webpush as _webpush, WebPushException
    PYWEBPUSH_OK = True
except Exception:  # noqa: BLE001 — ImportError и любые сбои окружения
    _webpush = None
    WebPushException = Exception
    PYWEBPUSH_OK = False

push_bp = Blueprint("push", __name__)


def _env_int(name, default, minimum=0):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


PUSH_ENABLED = os.getenv("PUSH_ENABLED", "true").strip().lower() != "false"
TICK_SECONDS = _env_int("PUSH_TICK_SECONDS", 30, 5)
LOOKBACK_MIN = _env_int("PUSH_LOOKBACK_MIN", 5, 1)
REPEAT_MIN = _env_int("PUSH_REPEAT_MIN", 15, 0)
SUPPRESS_BEFORE_MIN = _env_int("PUSH_SUPPRESS_BEFORE_MIN", 60, 0)
MAX_ATTEMPTS = _env_int("PUSH_MAX_ATTEMPTS", 3, 1)
MAX_SUBS_PER_USER = 10
TTL_SECONDS = 30 * 60          # устаревшее напоминание не должно приходить позже
TEST_TTL_SECONDS = 5 * 60
TEST_COOLDOWN_SECONDS = 20
SEND_TIMEOUT_SECONDS = 10

# ------------------------------------------------------------ допустимые хосты

_EXACT_HOSTS = {"fcm.googleapis.com", "updates.push.services.mozilla.com"}
_SUFFIX_HOSTS = (".push.apple.com", ".notify.windows.com")


def _allowed_hosts():
    exact, suffix = set(_EXACT_HOSTS), list(_SUFFIX_HOSTS)
    for item in os.getenv("PUSH_ALLOWED_HOSTS", "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if item.startswith("."):
            suffix.append(item)
        else:
            exact.add(item)
    return exact, tuple(suffix)


def _endpoint_allowed(url):
    """Сервер сам будет обращаться по этому адресу, поэтому принимаем только
    https-адреса известных push-сервисов (защита от SSRF)."""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return False
    if p.scheme != "https" or not host or p.username or p.password or port not in (None, 443):
        return False
    exact, suffix = _allowed_hosts()
    return host in exact or host.endswith(suffix)


_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_subscription(data):
    """-> (endpoint, p256dh, auth); ValueError с понятным текстом."""
    if not isinstance(data, dict):
        raise ValueError("Некорректный запрос")
    endpoint = data.get("endpoint")
    keys = data.get("keys")
    if not _endpoint_allowed(endpoint):
        raise ValueError("Адрес push-сервиса не поддерживается")
    if not isinstance(keys, dict):
        raise ValueError("Не переданы ключи подписки")
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(p256dh, str) and _B64URL.match(p256dh) and 80 <= len(p256dh) <= 100):
        raise ValueError("Некорректный ключ p256dh")
    if not (isinstance(auth, str) and _B64URL.match(auth) and 16 <= len(auth) <= 32):
        raise ValueError("Некорректный ключ auth")
    return endpoint, p256dh, auth


# ------------------------------------------------------------------ VAPID-ключ

_vapid_lock = threading.Lock()
_vapid_cache = {}


def _vapid_key_path():
    return os.getenv("VAPID_KEY_FILE") or os.path.join(os.path.dirname(_db.DATABASE) or ".", "vapid_private.pem")


def _vapid_subject():
    explicit = os.getenv("VAPID_SUBJECT", "").strip()
    if explicit:
        return explicit
    host = os.getenv("WA_RP_ID", "").strip()
    return f"https://{host}" if host else "https://localhost"


def _load_or_create_vapid():
    """Возвращает (путь к PEM, публичный ключ base64url) — создаёт ключ при первом запуске.
    Файл ключа лежит рядом с БД (том /data), права 600."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    with _vapid_lock:
        path = _vapid_key_path()
        cached = _vapid_cache.get(path)
        if cached:
            return path, cached

        key = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:  # другой процесс успел раньше
                with open(path, "rb") as f:
                    key = serialization.load_pem_private_key(f.read(), password=None)
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(pem)

        if not isinstance(key, ec.EllipticCurvePrivateKey) or key.curve.name != "secp256r1":
            raise ValueError("Ключ VAPID должен быть EC P-256")
        raw = key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        public_b64 = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        _vapid_cache[path] = public_b64
        return path, public_b64


def push_status():
    """-> (available, reason, public_key). reason: None | disabled | library_missing | key_error"""
    if not PUSH_ENABLED:
        return False, "disabled", None
    if not PYWEBPUSH_OK:
        return False, "library_missing", None
    try:
        _, public = _load_or_create_vapid()
    except Exception:  # noqa: BLE001
        return False, "key_error", None
    return True, None, public


# ------------------------------------------------------------------- отправка

class PushSendError(Exception):
    def __init__(self, status, message=""):
        super().__init__(message)
        self.status = status  # HTTP-статус push-сервиса или None (сеть/шифрование)

    @property
    def gone(self):
        return self.status in (404, 410)

    @property
    def transient(self):
        return self.status is None or self.status in (408, 429) or self.status >= 500


def _real_sender(subscription_info, body, ttl):
    """Единственное место, где вызывается pywebpush."""
    path, _ = _load_or_create_vapid()
    try:
        _webpush(
            subscription_info=subscription_info,
            data=body,
            vapid_private_key=path,
            vapid_claims={"sub": _vapid_subject()},  # словарь мутируется библиотекой — создаём заново
            ttl=ttl,
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except WebPushException as ex:
        status = getattr(ex, "status_code", None)
        if status is None:
            resp = getattr(ex, "response", None)
            status = getattr(resp, "status_code", None) if resp is not None else None
        raise PushSendError(status, str(ex)[:200]) from None
    except Exception as ex:  # noqa: BLE001 — сеть, шифрование и т.п.
        raise PushSendError(None, type(ex).__name__) from None


def send_to_user(user_id, payload, ttl=TTL_SECONDS, sender=None):
    """Отправляет payload на все устройства пользователя.
    -> {"sent": n, "removed": n, "failed": n, "transient": n}"""
    sender = sender or _real_sender
    db = get_db()
    subs = db.execute(
        "SELECT id, endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?", (user_id,)
    ).fetchall()
    body = json.dumps(payload, ensure_ascii=False)
    result = {"sent": 0, "removed": 0, "failed": 0, "transient": 0}
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": {"p256dh": s["p256dh"], "auth": s["auth"]}}
        try:
            sender(info, body, ttl)
        except PushSendError as e:
            if e.gone:
                db.execute("DELETE FROM push_subscriptions WHERE id = ?", (s["id"],))
                result["removed"] += 1
            else:
                key = "transient" if e.transient else "failed"
                result[key] += 1
                db.execute(
                    "UPDATE push_subscriptions SET failure_count = failure_count + 1, last_error = ? WHERE id = ?",
                    (f"{e.status or 'net'}: {str(e)[:120]}", s["id"]),
                )
        else:
            result["sent"] += 1
            db.execute(
                "UPDATE push_subscriptions SET last_success_at = datetime('now'), failure_count = 0, last_error = NULL WHERE id = ?",
                (s["id"],),
            )
    db.commit()
    return result


# --------------------------------------------------------------------- маршруты

@push_bp.get("/api/push/config")
@login_required
def api_push_config():
    available, reason, public = push_status()
    count = get_db().execute(
        "SELECT COUNT(*) AS c FROM push_subscriptions WHERE user_id = ?", (session["user_id"],)
    ).fetchone()["c"]
    return jsonify(available=available, reason=reason, public_key=public, subscriptions=count)


@push_bp.post("/api/push/subscribe")
@login_required
def api_push_subscribe():
    data = request.get_json(silent=True)
    try:
        endpoint, p256dh, auth = _validate_subscription(data)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db = get_db()
    exists = db.execute("SELECT user_id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
    if not exists:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM push_subscriptions WHERE user_id = ?", (session["user_id"],)
        ).fetchone()["c"]
        if count >= MAX_SUBS_PER_USER:
            return jsonify(error=f"Достигнут предел: {MAX_SUBS_PER_USER} устройств"), 400

    # Endpoint уникален. Если на этом устройстве вошёл другой пользователь,
    # подписка переходит к нему — уведомления предыдущего не должны показываться.
    db.execute(
        "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, user_agent) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, "
        "auth = excluded.auth, user_agent = excluded.user_agent, failure_count = 0, last_error = NULL",
        (session["user_id"], endpoint, p256dh, auth, request.headers.get("User-Agent", "")[:255]),
    )
    db.commit()
    audit("push_subscribe", "push_subscriptions", None, {})  # endpoint — секретный адрес, в журнал не пишем
    return jsonify(ok=True)


@push_bp.post("/api/push/unsubscribe")
@login_required
def api_push_unsubscribe():
    data = request.get_json(silent=True)
    endpoint = data.get("endpoint") if isinstance(data, dict) else None
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > 2048:
        return jsonify(error="Некорректный запрос"), 400
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?", (endpoint, session["user_id"]))
    db.commit()
    audit("push_unsubscribe", "push_subscriptions", None, {})
    return jsonify(ok=True)


_last_test = {}
_last_test_lock = threading.Lock()


@push_bp.post("/api/push/test")
@login_required
def api_push_test():
    available, reason, _ = push_status()
    if not available:
        return jsonify(error="Уведомления на сервере недоступны"), 503
    uid = session["user_id"]
    with _last_test_lock:
        now = time.time()
        if now - _last_test.get(uid, 0) < TEST_COOLDOWN_SECONDS:
            return jsonify(error="Подождите немного перед повторной отправкой"), 429
        _last_test[uid] = now
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM push_subscriptions WHERE user_id = ?", (uid,)).fetchone()["c"]
    if not count:
        return jsonify(error="Нет подключённых устройств. Сначала включите уведомления"), 400
    res = send_to_user(uid, {
        "title": "Медицинский дневник",
        "body": "Тестовое уведомление: всё работает.",
        "tag": "test",
        "tab": "meds",
    }, ttl=TEST_TTL_SECONDS, sender=_current_sender())
    audit("push_test", "push_subscriptions", None, res)
    return jsonify(ok=res["sent"] > 0, **res)


# Точка подмены отправителя для тестов.
_sender_override = None


def _current_sender():
    return _sender_override or _real_sender


# ----------------------------------------------------------------- планировщик

KIND_TITLES = {
    "glucose": "Пора измерить глюкозу",
    "vitals": "Пора измерить давление",
    "weight": "Пора взвеситься",
    "temperature": "Пора измерить температуру",
    "food": "Пора записать приём пищи",
}
# Где искать уже сделанную запись, чтобы не напоминать зря.
ENTRY_TABLES = {
    "glucose": ("glucose_entries", "measured_at"),
    "vitals": ("blood_pressure_entries", "measured_at"),
    "weight": ("weight_entries", "measured_at"),
    "temperature": ("temperature_entries", "measured_at"),
    "food": ("food_entries", "consumed_at"),
}
FMT = "%Y-%m-%d %H:%M:%S"


def _claim(db, user_id, kind, ref_id, due_at, phase):
    """Занимает событие. -> id строки или None, если уже обработано/занято."""
    try:
        cur = db.execute(
            "INSERT INTO notification_deliveries (user_id, kind, ref_id, due_at, phase, status, attempts) "
            "VALUES (?, ?, ?, ?, ?, 'claimed', 1)",
            (user_id, kind, ref_id, due_at, phase),
        )
        db.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
    # Повтор допускается только для строки в состоянии retry.
    cur = db.execute(
        "UPDATE notification_deliveries SET status = 'claimed', attempts = attempts + 1, updated_at = datetime('now') "
        "WHERE kind = ? AND ref_id = ? AND due_at = ? AND phase = ? AND status = 'retry' AND attempts < ?",
        (kind, ref_id, due_at, phase, MAX_ATTEMPTS),
    )
    db.commit()
    if cur.rowcount != 1:
        return None
    return db.execute(
        "SELECT id FROM notification_deliveries WHERE kind = ? AND ref_id = ? AND due_at = ? AND phase = ?",
        (kind, ref_id, due_at, phase),
    ).fetchone()["id"]


def _finish(db, row_ids, status, sent=0):
    for rid in row_ids:
        db.execute(
            "UPDATE notification_deliveries SET status = ?, sent_count = ?, updated_at = datetime('now') WHERE id = ?",
            (status, sent, rid),
        )
    db.commit()


def _has_subscriptions(db, user_id):
    return db.execute("SELECT 1 FROM push_subscriptions WHERE user_id = ? LIMIT 1", (user_id,)).fetchone() is not None


def _deliver(db, user_id, row_ids, payload, sender):
    res = send_to_user(user_id, payload, sender=sender)
    row_ids = list(row_ids)
    if res["sent"] > 0:
        _finish(db, row_ids, "sent", res["sent"])
    elif res["transient"] > 0:
        # Ещё есть попытки — 'retry', иначе итог 'failed'. Число попыток хранится в строке.
        for rid in row_ids:
            attempts = db.execute("SELECT attempts FROM notification_deliveries WHERE id = ?", (rid,)).fetchone()["attempts"]
            db.execute(
                "UPDATE notification_deliveries SET status = ?, updated_at = datetime('now') WHERE id = ?",
                ("retry" if attempts < MAX_ATTEMPTS else "failed", rid),
            )
        db.commit()
    else:
        _finish(db, row_ids, "failed", 0)
    return res


def _candidate_dates(now):
    earliest = now - timedelta(minutes=LOOKBACK_MIN + REPEAT_MIN)
    return sorted({earliest.date(), now.date()})


def _medication_events(db, now):
    """-> {(user_id, due_at, phase): [medication_id, ...]} для окна [now-LOOKBACK, now]."""
    start = now - timedelta(minutes=LOOKBACK_MIN)
    groups = {}
    for day in _candidate_dates(now):
        day_s = day.isoformat()
        rows = db.execute(
            "SELECT m.id, m.user_id, m.days_mask, s.time_of_day FROM medications m "
            "JOIN medication_schedule s ON s.medication_id = m.id "
            "WHERE m.deleted_at IS NULL AND m.is_active = 1 AND m.start_date <= ? "
            "AND (m.end_date IS NULL OR m.end_date >= ?)",
            (day_s, day_s),
        ).fetchall()
        for r in rows:
            if not r["days_mask"] & (1 << day.weekday()):
                continue
            slot = datetime.strptime(f"{day_s} {r['time_of_day']}:00", FMT)
            if start < slot <= now:
                groups.setdefault((r["user_id"], slot.strftime(FMT), "first"), []).append(r["id"])
            if REPEAT_MIN > 0 and start < slot + timedelta(minutes=REPEAT_MIN) <= now:
                groups.setdefault((r["user_id"], slot.strftime(FMT), "repeat"), []).append(r["id"])
    return groups


def _process_medications(db, now, sender):
    stats = {"sent": 0, "suppressed": 0}
    for (user_id, due_at, phase), med_ids in _medication_events(db, now).items():
        if not _has_subscriptions(db, user_id):
            continue
        claimed = []
        for med_id in sorted(set(med_ids)):
            recorded = db.execute(
                "SELECT 1 FROM medication_intakes WHERE medication_id = ? AND scheduled_at = ? AND deleted_at IS NULL",
                (med_id, due_at),
            ).fetchone()
            rid = _claim(db, user_id, "medication", med_id, due_at, phase)
            if rid is None:
                continue
            if recorded:
                _finish(db, [rid], "suppressed")
                stats["suppressed"] += 1
            else:
                claimed.append(rid)
        if not claimed:
            continue
        n = len(claimed)
        payload = {
            "title": "Пора принять лекарство" if n == 1 else "Пора принять лекарства",
            "body": ("Откройте дневник и отметьте приём." if phase == "first"
                     else "Приём ещё не отмечен. Откройте дневник."),
            "tag": f"med-{user_id}-{due_at}",
            "tab": "meds",
        }
        res = _deliver(db, user_id, claimed, payload, sender)
        stats["sent"] += res["sent"]
    return stats


def _process_reminders(db, now, sender):
    stats = {"sent": 0, "suppressed": 0}
    start = now - timedelta(minutes=LOOKBACK_MIN)
    rows = db.execute(
        "SELECT id, user_id, kind, title, time_of_day, days_mask FROM reminders "
        "WHERE deleted_at IS NULL AND is_active = 1"
    ).fetchall()
    for day in _candidate_dates(now):
        for r in rows:
            if not r["days_mask"] & (1 << day.weekday()):
                continue
            slot = datetime.strptime(f"{day.isoformat()} {r['time_of_day']}:00", FMT)
            if not (start < slot <= now):
                continue
            if not _has_subscriptions(db, r["user_id"]):
                continue
            due_at = slot.strftime(FMT)
            rid = _claim(db, r["user_id"], "reminder", r["id"], due_at, "first")
            if rid is None:
                continue
            if r["kind"] in ENTRY_TABLES:
                table, col = ENTRY_TABLES[r["kind"]]
                lo = (slot - timedelta(minutes=SUPPRESS_BEFORE_MIN)).strftime(FMT)
                done = db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id = ? AND deleted_at IS NULL AND {col} >= ? AND {col} <= ? LIMIT 1",
                    (r["user_id"], lo, now.strftime(FMT)),
                ).fetchone()
                if done:
                    _finish(db, [rid], "suppressed")
                    stats["suppressed"] += 1
                    continue
            title = r["title"] if r["kind"] == "custom" else KIND_TITLES.get(r["kind"], "Напоминание")
            payload = {
                "title": title,
                "body": "Откройте дневник, чтобы сделать запись.",
                "tag": f"rem-{r['id']}-{due_at}",
                "tab": "input",
            }
            res = _deliver(db, r["user_id"], [rid], payload, sender)
            stats["sent"] += res["sent"]
    return stats


_last_cleanup = {"at": 0.0}


def run_tick(now=None, sender=None):
    """Один проход планировщика. Вызывается внутри контекста приложения.
    now и sender можно подменить (тесты)."""
    available, _, _ = push_status() if sender is None else (True, None, None)
    if not available:
        return None
    now = (now or datetime.now()).replace(microsecond=0)
    sender = sender or _current_sender()
    db = get_db()
    med = _process_medications(db, now, sender)
    rem = _process_reminders(db, now, sender)
    if time.time() - _last_cleanup["at"] > 3600:
        _last_cleanup["at"] = time.time()
        db.execute("DELETE FROM notification_deliveries WHERE created_at < datetime('now', '-30 days')")
        db.commit()
    return {"medication": med, "reminders": rem}


_scheduler_started = False


def start_push_scheduler(app):
    """Запускает фоновый поток (один на процесс). Ошибки одного прохода не останавливают поток."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    if not PUSH_ENABLED:
        print("[push] Уведомления отключены (PUSH_ENABLED=false)", flush=True)
        return
    if not PYWEBPUSH_OK:
        print("[push] Модуль pywebpush не установлен — уведомления недоступны (нужна пересборка образа)", flush=True)
    else:
        print(f"[push] Планировщик уведомлений запущен: проверка каждые {TICK_SECONDS} с", flush=True)

    def loop():
        while True:
            try:
                with app.app_context():
                    run_tick()
            except Exception as e:  # noqa: BLE001
                print(f"[push] Ошибка планировщика: {type(e).__name__}", flush=True)
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True, name="push-scheduler").start()
