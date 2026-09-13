import os
import sqlite3
import secrets
import hashlib
import json
import threading
import time
import fcntl
import glob
import urllib.request
import urllib.error
import urllib.parse
import uuid
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


UNIT_RU = {
    # Старые значения, которые могли сохраниться в БД.
    "g": "г", "gram": "г", "grams": "г",
    "kg": "кг", "kilogram": "кг", "kilograms": "кг",
    "mg": "мг", "milligram": "мг", "milligrams": "мг",
    "mcg": "мкг", "µg": "мкг",
    "ml": "мл", "milliliter": "мл", "milliliters": "мл",
    "l": "л", "liter": "л", "liters": "л",
    "pcs": "шт", "pc": "шт", "piece": "шт", "pieces": "шт",
    "portion": "порция", "portions": "порция",
    # Русские значения уже корректны — оставляем их неизменными.
    "г": "г", "кг": "кг", "мг": "мг", "мкг": "мкг",
    "мл": "мл", "л": "л", "шт": "шт", "порция": "порция",
}

def unit_ru(value):
    """Возвращает безопасное русское обозначение единицы продукта."""
    key = str(value or "").strip()
    return UNIT_RU.get(key, key)

DEFAULT_RANGES = {
    "glucose_fasting": (3.3, 5.5),
    "glucose_post": (3.3, 7.8),
    "systolic": (90, 120),
    "diastolic": (60, 80),
    "pulse": (60, 100),
}
REF_RANGES = DEFAULT_RANGES  # используется как запасной вариант, если у пользователя нет своих границ

STATUS_COLORS = {"low": "#fff3c4", "ok": "#d9f2d9", "high": "#fbd9d9"}
STATUS_ICON = {"low": "\u25bc", "ok": "", "high": "\u25b2"}  # ▼ ниже / (без иконки) норма / ▲ выше
DEFAULT_SETTINGS = {"glucose": True, "vitals": True, "food": True, "temperature": True, "weight": True, "ranges_default": True, "ai_enabled": True}


def status_of(value, low, high):
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "ok"


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
                    if 0 <= low < high <= 1000:
                        ranges[key] = (low, high)
        except Exception:
            pass
    return settings, ranges




def glucose_assessment(value_mmol_l, glucose_type, ranges=None):
    ranges = ranges or DEFAULT_RANGES
    try:
        value = float(value_mmol_l)
    except Exception:
        return "", "", "ok"

    key = "glucose_fasting" if glucose_type == "fasting" else "glucose_post"
    low, high = ranges.get(key, ranges["glucose_fasting"])
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


def vitals_assessment(systolic, diastolic, pulse, ranges=None):
    ranges = ranges or DEFAULT_RANGES
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

    s_st = status_of(s, *ranges["systolic"])
    d_st = status_of(d, *ranges["diastolic"])
    p_st = status_of(p, *ranges["pulse"]) if p is not None else None

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


def temperature_assessment(value_c):
    """Справочная оценка температуры без постановки диагноза."""
    try:
        value = float(value_c)
    except (TypeError, ValueError):
        return "Значение температуры не распознано", "Проверьте измерение и единицы (°C).", "ok"

    if value < 35.0:
        return (
            "Температура ниже ориентировочного диапазона",
            "Повторите измерение и оцените самочувствие; при выраженной слабости или ухудшении состояния обратитесь за медицинской помощью.",
            "low",
        )
    if value <= 37.0:
        return "Температура в пределах ориентировочного диапазона", "Продолжайте наблюдение с учётом самочувствия.", "ok"
    if value < 38.0:
        return (
            "Температура повышена относительно ориентировочного диапазона",
            "Повторите измерение через некоторое время и наблюдайте за самочувствием; при сохранении повышения обратитесь к врачу.",
            "high",
        )
    return (
        "Температура высокая",
        "Повторите измерение и оцените самочувствие; при сохранении высокой температуры или ухудшении состояния обратитесь за медицинской помощью.",
        "high",
    )


def food_assessment():
    return (
        "Запись о питании",
        "Сопоставляйте время и количество с уровнем глюкозы и рекомендациями вашего врача.",
        "ok",
    )


def weight_assessment():
    # Вес без роста, возраста и целей пациента не диагностируется — это
    # только фиксация динамики. Универсального "нормального диапазона"
    # намеренно нет, в отличие от глюкозы/давления/температуры.
    return (
        "Запись веса",
        "Оценивайте динамику веса вместе с врачом; отдельное значение не является нормой или отклонением.",
        "ok",
    )



GIGACHAT_AI_ENABLED = os.getenv("GIGACHAT_AI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY", "").strip()
# Для физлиц GigaChat 3 Ultra доступен в Freemium-режиме; при необходимости
# модель можно заменить через GIGACHAT_MODEL без изменения кода.
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-3-Ultra").strip() or "GigaChat-3-Ultra"
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip() or "GIGACHAT_API_PERS"
GIGACHAT_TIMEOUT_SECONDS = max(5, min(60, int(os.getenv("GIGACHAT_TIMEOUT_SECONDS", "20"))))
GIGACHAT_MAX_ENTRIES = max(1, min(200, int(os.getenv("GIGACHAT_MAX_ENTRIES", "120"))))
GIGACHAT_TOKEN_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_API_URL = "https://api.giga.chat/v1/chat/completions"

_gigachat_token_lock = threading.Lock()
_gigachat_access_token = None
_gigachat_token_expires_at = 0.0


def _gigachat_get_access_token():
    """Получает и кратковременно кеширует OAuth access token GigaChat.

    GIGACHAT_AUTH_KEY — authorization key из кабинета GigaChat API.
    Сам access token действует ограниченное время, поэтому в окружении
    хранится только ключ авторизации, а не временный токен.
    """
    global _gigachat_access_token, _gigachat_token_expires_at

    if not GIGACHAT_AUTH_KEY:
        return None

    now = time.time()
    if _gigachat_access_token and now < _gigachat_token_expires_at - 60:
        return _gigachat_access_token

    with _gigachat_token_lock:
        now = time.time()
        if _gigachat_access_token and now < _gigachat_token_expires_at - 60:
            return _gigachat_access_token

        request = urllib.request.Request(
            GIGACHAT_TOKEN_URL,
            data=("scope=" + urllib.parse.quote_plus(GIGACHAT_SCOPE)).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": "Basic " + GIGACHAT_AUTH_KEY,
                "User-Agent": "MedicalDiary/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=GIGACHAT_TIMEOUT_SECONDS) as response:
            raw = response.read(64 * 1024)

        result = json.loads(raw.decode("utf-8"))
        token = str(result.get("access_token") or "").strip()
        if not token:
            raise ValueError("GigaChat не вернул access_token")

        try:
            raw_expires_at = float(result.get("expires_at"))
        except (TypeError, ValueError):
            raw_expires_at = None

        if raw_expires_at is None:
            expires_at = time.time() + 1500
        else:
            # ВАЖНО: GigaChat возвращает expires_at как Unix-время в
            # МИЛЛИСЕКУНДАХ, а не в секундах (подтверждено документацией
            # Sber). При использовании этого значения "как есть" оно почти
            # в 1000 раз больше текущего time.time(), поэтому кэш токена
            # выглядел валидным ещё десятки тысяч лет вперёд и никогда не
            # обновлялся — а реальный токен GigaChat живёт всего 30 минут.
            # Итог: первый запрос после старта контейнера работал (токен
            # только что получен), а на следующий день все запросы падали
            # с HTTP 401, и это не лечилось ничем, кроме перезапуска
            # контейнера (который сбрасывает кэш через глобальные
            # переменные модуля).
            expires_at = raw_expires_at / 1000.0
            # Доп. подстраховка: реальный токен живёт ~30 минут, поэтому
            # если после конвертации получили больше часа от текущего
            # момента — формат ответа не тот, что мы ожидаем, и лучше
            # перестраховаться коротким временем жизни, чем снова
            # закэшировать токен на неопределённо долгий срок.
            if expires_at > time.time() + 3600:
                expires_at = time.time() + 1500

        _gigachat_access_token = token
        _gigachat_token_expires_at = expires_at
        return token


def _gigachat_invalidate_token():
    """Сбрасывает закэшированный OAuth-токен GigaChat.

    Вызывается при получении HTTP 401 от самого GigaChat: если сервер
    говорит, что токен невалиден, значит наше представление о его сроке
    действия разошлось с реальностью (например, токен отозван раньше
    срока) — не дожидаясь перезапуска контейнера, следующий вызов
    _gigachat_get_access_token() получит новый токен.
    """
    global _gigachat_access_token, _gigachat_token_expires_at
    with _gigachat_token_lock:
        _gigachat_access_token = None
        _gigachat_token_expires_at = 0.0


def _normalize_ai_text(value, max_chars):
    """Убирает переносы/лишние пробелы и жёстко ограничивает размер текста."""
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:max_chars].rstrip()


def _ai_entry_context(entry, index):
    """Возвращает только минимальные данные записи, необходимые для AI-оценки.

    Персональные данные, комментарии пользователя и идентификаторы в запрос
    GigaChat не передаются.
    """
    result = {
        "index": index,
        "type": entry.get("type"),
        "measured_at": entry.get("measured_at"),
    }

    entry_type = entry.get("type")
    if entry_type == "glucose":
        result.update({
            "glucose_type": entry.get("glucose_type"),
            "value_mmol_l": entry.get("value_mmol_l"),
        })
    elif entry_type == "vitals":
        result.update({
            "systolic_mmhg": entry.get("systolic_mmhg"),
            "diastolic_mmhg": entry.get("diastolic_mmhg"),
            "pulse_bpm": entry.get("pulse_bpm"),
        })
    elif entry_type == "food":
        result.update({
            "food_name": entry.get("food_name"),
            "amount_value": entry.get("amount_value"),
            "amount_unit": entry.get("amount_unit"),
        })
    elif entry_type == "temperature":
        result.update({
            "temperature_c": entry.get("temperature_c"),
        })
    elif entry_type == "weight":
        result.update({
            "weight_kg": entry.get("weight_kg"),
        })

    return result


def _ai_ranges_payload(ranges):
    """Единый набор справочных диапазонов, отправляемых в GigaChat.

    Вынесено в отдельную функцию, чтобы ХЭШ кэша (см. _ai_input_hash) и
    сам запрос к модели гарантированно использовали одни и те же данные —
    иначе кэш мог бы считаться валидным, даже если реальные диапазоны,
    отправленные модели в прошлый раз, отличались.
    """
    return {
        "glucose_fasting_mmol_l": list(ranges.get("glucose_fasting", DEFAULT_RANGES["glucose_fasting"])),
        "glucose_post_mmol_l": list(ranges.get("glucose_post", DEFAULT_RANGES["glucose_post"])),
        "systolic_mmhg": list(ranges.get("systolic", DEFAULT_RANGES["systolic"])),
        "diastolic_mmhg": list(ranges.get("diastolic", DEFAULT_RANGES["diastolic"])),
        "pulse_bpm": list(ranges.get("pulse", DEFAULT_RANGES["pulse"])),
        "temperature_c": [35.0, 37.0],
    }


def _ai_input_hash(context, ranges_payload):
    """Хэш входных данных, отправляемых в GigaChat для одной записи.

    Используется как ключ кэша (ai_assessment_cache.input_hash): пока
    значение, тип, дата/время записи и используемые диапазоны не
    изменились — повторный запрос к модели не нужен, берётся сохранённый
    текст. Комментарии и ID записи в хэш не входят, т.к. они и так не
    передаются модели (см. _ai_entry_context).
    """
    ctx = dict(context)
    ctx.pop("index", None)
    blob = json.dumps(
        {"entry": ctx, "ranges": ranges_payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _ai_blocked_by_safety_override(entry):
    """Записи с критическим давлением всегда получают встроенную
    экстренную формулировку — ни свежая, ни закэшированная оценка ИИ их
    не заменяет, поэтому такие записи в AI-обработку не отправляются."""
    if entry.get("type") == "vitals":
        try:
            return int(entry.get("systolic_mmhg")) >= 180 or int(entry.get("diastolic_mmhg")) >= 120
        except (TypeError, ValueError):
            return False
    return False


def _gigachat_generate_assessments(candidates, ranges_payload, enabled=True):
    """Генерирует краткие оценки через GigaChat пакетами.

    candidates — список кортежей (global_index, entry, context), уже
    отфильтрованный вызывающей стороной (add_ai_assessments) от записей,
    для которых нашёлся валидный кэш или которые исключены по
    соображениям безопасности. ranges_payload — тот же словарь
    диапазонов, что использовался для расчёта хэша кэша (см.
    _ai_ranges_payload), чтобы кэш и реальный запрос не могли разойтись.

    AI не получает персональные данные, комментарии пользователя или ID.
    Ошибка любого AI-запроса не блокирует формирование PDF.
    Возвращает {global_index: (assessment, recommendation)}.
    """
    if not (enabled and GIGACHAT_AI_ENABLED and GIGACHAT_AUTH_KEY and candidates):
        return {}

    selected = candidates[:GIGACHAT_MAX_ENTRIES]
    batch_size = 25
    generated = {}

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "assessment": {"type": "string", "maxLength": 160},
                        "recommendation": {"type": "string", "maxLength": 300},
                    },
                    "required": ["index", "assessment", "recommendation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    system_prompt = (
        "Ты формируешь только безопасный текст для медицинского дневника. "
        "Не ставь диагнозы и не назначай, не отменяй и не изменяй лекарства или лечение. "
        "Используй только переданные данные и ориентировочные диапазоны. "
        "Не придумывай отсутствующие факты и не используй внешние источники. "
        "Для каждой записи верни нейтральную оценку и одно безопасное наблюдательное действие. "
        "assessment — не более 160 символов; recommendation — не более 300 символов. "
        "Оба текста должны быть одной строкой, без переносов. "
        "Не повторяй все исходные данные в рекомендации. "
        "При систолическом давлении >=180 или диастолическом >=120 не смягчай рекомендацию "
        "обратиться за медицинской помощью. "
        "Если данных недостаточно, прямо укажи это, не делая предположений. "
        "Ответь только JSON по заданной схеме."
    )

    try:
        token = _gigachat_get_access_token()
        if not token:
            return {}

        for batch_start in range(0, len(selected), batch_size):
            batch = selected[batch_start:batch_start + batch_size]

            payload_entries = []
            for local_i, (_global_index, _entry, context) in enumerate(batch):
                sendctx = dict(context)
                sendctx["index"] = local_i
                payload_entries.append(sendctx)

            payload = {"entries": payload_entries, "ranges": ranges_payload}

            prompt = (
                system_prompt
                + "\n\nДанные:\n"
                + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )

            body = {
                "model": GIGACHAT_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 1800,
                "response_format": {
                    "type": "json_schema",
                    "schema": schema,
                    "strict": True,
                },
            }

            http_request = urllib.request.Request(
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

            with urllib.request.urlopen(http_request, timeout=GIGACHAT_TIMEOUT_SECONDS) as response:
                raw = response.read(512 * 1024)

            result = json.loads(raw.decode("utf-8"))
            text = result["choices"][0]["message"]["content"]
            parsed = text if isinstance(text, dict) else json.loads(text)
            items = parsed.get("items")
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    local_index = int(item.get("index"))
                except (TypeError, ValueError):
                    continue
                if local_index < 0 or local_index >= len(batch):
                    continue

                global_index = batch[local_index][0]
                if global_index in generated:
                    continue

                assessment = _normalize_ai_text(item.get("assessment"), 160)
                recommendation = _normalize_ai_text(item.get("recommendation"), 300)
                if assessment and recommendation:
                    generated[global_index] = (assessment, recommendation)

    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
            _gigachat_invalidate_token()
        print(f"[gigachat] assessment generation failed: {type(exc).__name__}: {exc}", flush=True)

    return generated


def add_ai_assessments(entries, ranges=None, enabled=True):
    """Накладывает формулировки GigaChat поверх детерминированной оценки.

    Перед обращением к GigaChat каждая запись проверяется по кэшу
    (таблица ai_assessment_cache): если хэш входных данных (тип,
    значение, дата/время записи + справочные диапазоны) не изменился с
    прошлого раза — используется сохранённый текст без обращения к
    модели. Это даёт одинаковый текст при повторном экспорте PDF и не
    тратит лимиты GigaChat на записи, которые не менялись. Если запись
    отредактирована или диапазоны изменены — хэш не совпадёт, и оценка
    будет сгенерирована заново автоматически.
    """
    ranges = ranges or DEFAULT_RANGES
    if not (enabled and GIGACHAT_AI_ENABLED and GIGACHAT_AUTH_KEY and entries):
        return entries, False

    ranges_payload = _ai_ranges_payload(ranges)
    user_id = session.get("user_id")
    db = get_db()

    cache_rows = {}
    if user_id:
        try:
            rows = db.execute(
                "SELECT entry_type, entry_id, input_hash, assessment, recommendation "
                "FROM ai_assessment_cache WHERE user_id = ?",
                (user_id,),
            ).fetchall()
            cache_rows = {(r["entry_type"], r["entry_id"]): r for r in rows}
        except Exception:
            cache_rows = {}

    used = False
    candidates = []  # (global_index, entry, context, input_hash) — требуют обращения к GigaChat

    for i, e in enumerate(entries):
        if _ai_blocked_by_safety_override(e):
            continue
        try:
            context = _ai_entry_context(e, i)
        except Exception:
            continue

        input_hash = _ai_input_hash(context, ranges_payload)
        cached = cache_rows.get((e.get("type"), e.get("id")))
        if cached and cached["input_hash"] == input_hash:
            e["assessment"] = cached["assessment"]
            e["recommendation"] = cached["recommendation"]
            e["assessment_source"] = "ai"
            used = True
        else:
            candidates.append((i, e, context, input_hash))

    if candidates:
        generated = _gigachat_generate_assessments(
            [(idx, e, ctx) for idx, e, ctx, _h in candidates],
            ranges_payload,
            enabled=enabled,
        )
        hash_by_index = {idx: h for idx, _e, _ctx, h in candidates}

        for index, (assessment, recommendation) in generated.items():
            e = entries[index]
            e["assessment"] = assessment
            e["recommendation"] = recommendation
            e["assessment_source"] = "ai"
            used = True

            if user_id and e.get("id") is not None:
                try:
                    db.execute(
                        """
                        INSERT INTO ai_assessment_cache
                            (user_id, entry_type, entry_id, input_hash, assessment, recommendation, model, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(user_id, entry_type, entry_id) DO UPDATE SET
                            input_hash = excluded.input_hash,
                            assessment = excluded.assessment,
                            recommendation = excluded.recommendation,
                            model = excluded.model,
                            updated_at = datetime('now')
                        """,
                        (
                            user_id, e.get("type"), e.get("id"), hash_by_index[index],
                            assessment, recommendation, GIGACHAT_MODEL,
                        ),
                    )
                except Exception:
                    pass

        if user_id:
            try:
                db.commit()
            except Exception:
                pass

    return entries, used


def add_assessments(entries, ranges=None):
    ranges = ranges or DEFAULT_RANGES
    for e in entries:
        try:
            if e.get("type") == "glucose":
                assessment, recommendation, status = glucose_assessment(
                    e.get("value_mmol_l"),
                    e.get("glucose_type", "fasting"),
                    ranges,
                )
            elif e.get("type") == "vitals":
                assessment, recommendation, status = vitals_assessment(
                    e.get("systolic_mmhg"),
                    e.get("diastolic_mmhg"),
                    e.get("pulse_bpm"),
                    ranges,
                )
            elif e.get("type") == "temperature":
                assessment, recommendation, status = temperature_assessment(e.get("temperature_c"))
            elif e.get("type") == "food":
                assessment, recommendation, status = food_assessment()
            elif e.get("type") == "weight":
                assessment, recommendation, status = weight_assessment()
            else:
                assessment, recommendation, status = "", "", "ok"

            e["assessment"] = assessment
            e["recommendation"] = recommendation
            e["assessment_status"] = status
            e["assessment_source"] = "rules"
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
  settings_json TEXT,
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

CREATE TABLE IF NOT EXISTS temperature_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  temperature_c REAL NOT NULL,
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_temperature_user_time ON temperature_entries(user_id, measured_at);

CREATE TABLE IF NOT EXISTS weight_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  measured_at TEXT NOT NULL,
  weight_kg REAL NOT NULL CHECK (weight_kg BETWEEN 1.0 AND 500.0),
  comment TEXT,
  source TEXT NOT NULL DEFAULT 'manual',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  deleted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_weight_user_time ON weight_entries(user_id, measured_at);

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

-- Кэш ИИ-оценок (GigaChat) по каждой записи дневника. input_hash — хэш
-- ровно тех данных, что отправляются модели (значение, тип, дата/время
-- записи + справочные диапазоны). Пока хэш совпадает — запрос к GigaChat
-- повторно не делается, при экспорте PDF используется сохранённый текст.
-- Если пользователь изменит запись или свои диапазоны, хэш изменится, и
-- при следующем экспорте оценка будет сгенерирована заново автоматически.
CREATE TABLE IF NOT EXISTS ai_assessment_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  entry_type TEXT NOT NULL,
  entry_id INTEGER NOT NULL,
  input_hash TEXT NOT NULL,
  assessment TEXT NOT NULL,
  recommendation TEXT NOT NULL,
  model TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (user_id, entry_type, entry_id)
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
    if "settings_json" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN settings_json TEXT")

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


BACKUP_ENABLED = os.getenv("BACKUP_ENABLED", "true").lower() not in ("0", "false", "no")
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(DATABASE) or ".", "backups"))
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "3"))
BACKUP_MINUTE = int(os.getenv("BACKUP_MINUTE", "0"))
BACKUP_RETENTION_DAYS = int(os.getenv("BACKUP_RETENTION_DAYS", "14"))


def cleanup_old_backups():
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    for path in glob.glob(os.path.join(BACKUP_DIR, "medical_diary-*.db")):
        try:
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                os.remove(path)
                print(f"[backup] Удалена устаревшая копия: {path}", flush=True)
        except OSError:
            pass


def backup_database(force=False):
    """Снимает полную копию базы через встроенный SQLite Backup API, а не
    простым копированием файла: при включённом WAL-режиме (он у нас
    включён) копирование файла напрямую может не захватить данные, ещё не
    перенесённые из WAL-журнала в основной файл, и дать повреждённую или
    неполную копию. backup() решает это на уровне самого движка SQLite.

    Возвращает путь к файлу копии, или None, если бэкап не потребовался
    (уже есть за сегодня) либо не удался.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    if force:
        # Ручной запуск получает точную метку времени, а не только дату —
        # чтобы не перезаписать тихо уже снятую сегодня автоматическую
        # копию и чтобы несколько ручных запусков не затирали друг друга.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        stamp = datetime.now().strftime("%Y%m%d")
    dest_path = os.path.join(BACKUP_DIR, f"medical_diary-{stamp}.db")

    if not force and os.path.exists(dest_path):
        # За сегодня копия уже есть (например, воркер перезапускался) —
        # не делаем повторно.
        return dest_path

    lock_path = os.path.join(BACKUP_DIR, ".backup.lock")
    lock_fd = open(lock_path, "w")
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Бэкап прямо сейчас снимает другой воркер (при нескольких
            # gunicorn-воркерах у каждого свой поток-планировщик) — не
            # дублируем работу, просто выходим.
            return None

        if not force and os.path.exists(dest_path):
            return dest_path

        tmp_path = dest_path + ".tmp"
        src = sqlite3.connect(DATABASE)
        try:
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        os.replace(tmp_path, dest_path)
        cleanup_old_backups()
        print(f"[backup] Резервная копия БД создана: {dest_path}", flush=True)
        return dest_path
    except Exception as e:
        print(f"[backup] ОШИБКА резервного копирования: {e}", flush=True)
        return None
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fd.close()


def _sqlite_integrity_ok(path):
    """Проверяет целостность SQLite-файла в режиме только для чтения."""
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{os.path.abspath(path)}?mode=ro",
            uri=True,
            timeout=10,
        )
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    except (OSError, sqlite3.Error):
        return False
    finally:
        if conn is not None:
            conn.close()


def _backup_contains_admin(path, username):
    """Проверяет, что в копии сохранён активный текущий администратор."""
    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{os.path.abspath(path)}?mode=ro",
            uri=True,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT status, is_admin FROM users WHERE username = ? LIMIT 1",
            (username,),
        ).fetchone()
        return bool(
            row
            and row["status"] == "active"
            and int(row["is_admin"] or 0) == 1
        )
    except (OSError, sqlite3.Error):
        return False
    finally:
        if conn is not None:
            conn.close()


def restore_database_from_backup(backup_path, admin_username):
    """Безопасно восстанавливает рабочую БД из резервной копии.

    Резервная копия сначала проверяется, затем разворачивается во временный
    SQLite-файл через Backup API. Только после повторной проверки целостности
    временный файл атомарно заменяет рабочую БД.
    """
    backup_real = os.path.realpath(backup_path)
    backup_dir_real = os.path.realpath(BACKUP_DIR)
    database_real = os.path.realpath(DATABASE)

    if os.path.commonpath([backup_real, backup_dir_real]) != backup_dir_real:
        raise ValueError("Файл резервной копии находится вне каталога резервных копий")
    if backup_real == database_real:
        raise ValueError("Нельзя восстановить текущий файл базы как резервную копию")
    if not os.path.isfile(backup_real):
        raise ValueError("Резервная копия не найдена")
    if not _sqlite_integrity_ok(backup_real):
        raise ValueError("Резервная копия повреждена: integrity_check не пройден")
    if not _backup_contains_admin(backup_real, admin_username):
        raise ValueError(
            "В выбранной копии нет активного администратора с текущим логином. "
            "Восстановление остановлено, чтобы не потерять доступ."
        )

    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)) or ".", exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)

    lock_path = os.path.join(BACKUP_DIR, ".backup.lock")
    lock_fd = open(lock_path, "w")
    tmp_path = None

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Соединение текущего HTTP-запроса больше не должно держать старый файл.
        current_db = g.pop("db", None)
        if current_db is not None:
            current_db.close()

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        emergency_path = os.path.join(
            BACKUP_DIR,
            f"medical_diary-pre-restore-{stamp}-{secrets.token_hex(3)}.db",
        )

        # Сначала сохраняем аварийную копию текущего состояния.
        src = sqlite3.connect(DATABASE, timeout=30)
        try:
            emergency = sqlite3.connect(emergency_path)
            try:
                src.backup(emergency)
            finally:
                emergency.close()
        finally:
            src.close()

        if not _sqlite_integrity_ok(emergency_path):
            try:
                os.remove(emergency_path)
            except OSError:
                pass
            raise RuntimeError("Не удалось создать корректную аварийную копию текущей БД")

        tmp_path = os.path.abspath(DATABASE) + f".restore-{secrets.token_hex(8)}.tmp"

        src = sqlite3.connect(backup_real, timeout=30)
        try:
            dst = sqlite3.connect(tmp_path, timeout=30)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

        if not _sqlite_integrity_ok(tmp_path):
            raise RuntimeError("Временная восстановленная БД не прошла integrity_check")

        # WAL/SHM от прежнего файла не должны использоваться новой БД.
        for suffix in ("-wal", "-shm"):
            try:
                os.remove(DATABASE + suffix)
            except FileNotFoundError:
                pass

        os.replace(tmp_path, DATABASE)
        tmp_path = None

        # Применяем штатную схему/миграции приложения.
        init_db()

        return emergency_path
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def _seconds_until(hour, minute):
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def backup_scheduler_loop():
    while True:
        try:
            time.sleep(_seconds_until(BACKUP_HOUR, BACKUP_MINUTE))
            backup_database()
        except Exception as e:
            print(f"[backup] Ошибка в планировщике бэкапов: {e}", flush=True)
            time.sleep(60)


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


MAX_HISTORY_RANGE_DAYS = 366


VALID_ENTRY_TYPES = ("glucose", "vitals", "temperature", "weight", "food")


def _parse_entry_types(entry_type):
    """Разбирает параметр типа записей фильтра/PDF-экспорта.

    Принимает 'all' (все типы) либо список типов через запятую, например
    'glucose,weight,food' — так поддерживается выбор нескольких, но не всех,
    типов записей одновременно. Возвращает множество типов для выборки.
    """
    if entry_type == "all":
        return set(VALID_ENTRY_TYPES)

    parts = [p.strip() for p in entry_type.split(",") if p.strip()]
    if not parts or any(p not in VALID_ENTRY_TYPES for p in parts):
        raise ValueError("Некорректный тип фильтра")

    return set(parts)


def query_entries(user_id, date_from=None, date_to=None, entry_type="all", sort="date", ranges=None):
    selected_types = _parse_entry_types(entry_type)
    if sort not in ("date", "value"):
        raise ValueError("Некорректный порядок сортировки")

    ranges = ranges or DEFAULT_RANGES

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

    if "glucose" in selected_types:
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
            low, high = ranges["glucose_fasting" if r["glucose_type"] == "fasting" else "glucose_post"]
            st = status_of(val, low, high)
            icon = STATUS_ICON[st]
            entries.append(
                {
                    "id": r["id"],
                    "type": "glucose",
                    "type_label": "Глюкоза",
                    "measured_at": r["measured_at"],
                "measured_at_ru": format_dt_ru(r["measured_at"]),
                    "display": f"{val:.1f} ммоль/л ({glucose_type_label})",
                    "display_html": f'<span class="st-{st}">{val:.1f}{icon}</span> ммоль/л ({glucose_type_label})',
                    "display_pdf": f'<font backcolor="{STATUS_COLORS[st]}">{val:.1f}{icon}</font> ммоль/л ({glucose_type_label})',
                    "comment": r["comment"] or "",
                    "sort_value": val,
                    "glucose_type": r["glucose_type"],
                    "value_mmol_l": val,
                    "status": st,
                }
            )

    if "vitals" in selected_types:
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
            st_s = status_of(s, *ranges["systolic"])
            st_d = status_of(d, *ranges["diastolic"])
            icon_s = STATUS_ICON[st_s]
            icon_d = STATUS_ICON[st_d]
            display = f"{s}/{d} мм рт. ст."
            display_html = f'<span class="st-{st_s}">{s}{icon_s}</span>/<span class="st-{st_d}">{d}{icon_d}</span> мм рт. ст.'
            display_pdf = f'<font backcolor="{STATUS_COLORS[st_s]}">{s}{icon_s}</font>/<font backcolor="{STATUS_COLORS[st_d]}">{d}{icon_d}</font> мм рт. ст.'
            overall_st = "high" if ("high" in (st_s, st_d)) else ("low" if "low" in (st_s, st_d) else "ok")
            if p is not None:
                p = int(p)
                st_p = status_of(p, *ranges["pulse"])
                icon_p = STATUS_ICON[st_p]
                display += f", пульс {p}"
                display_html += f', пульс <span class="st-{st_p}">{p}{icon_p}</span>'
                display_pdf += f', пульс <font backcolor="{STATUS_COLORS[st_p]}">{p}{icon_p}</font>'
            entries.append(
                {
                    "id": r["id"],
                    "type": "vitals",
                    "type_label": "Давление/пульс",
                    "measured_at": r["measured_at"],
                "measured_at_ru": format_dt_ru(r["measured_at"]),
                    "display": display,
                    "display_html": display_html,
                    "display_pdf": display_pdf,
                    "comment": r["comment"] or "",
                    "sort_value": float(s),
                    "systolic_mmhg": s,
                    "diastolic_mmhg": d,
                    "pulse_bpm": p,
                    "status": overall_st,
                    "systolic_status": st_s,
                    "diastolic_status": st_d,
                    "pulse_status": st_p if p is not None else None,
                }
            )

    if "food" in selected_types:
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
                "measured_at_ru": format_dt_ru(r["consumed_at"]),
                    "display": f"{r['food_name']} — {float(r['amount_value']):g} {UNIT_RU.get(r['amount_unit'], r['amount_unit'])}",
                    "comment": r["comment"] or "",
                    "sort_value": float(r["amount_value"]),
                    "food_name": r["food_name"],
                    "amount_value": float(r["amount_value"]),
                    "amount_unit": unit_ru(r["amount_unit"]),
                    "status": "ok",
                }
            )

    if "temperature" in selected_types:
        rows = db.execute(
            """
            SELECT *
            FROM temperature_entries
            WHERE user_id = ?
              AND deleted_at IS NULL
              AND date(measured_at) >= date(?)
              AND date(measured_at) <= date(?)
            ORDER BY measured_at DESC
            """,
            (user_id, d_from, d_to),
        ).fetchall()

        for r in rows:
            val = float(r["temperature_c"])
            entries.append(
                {
                    "id": r["id"],
                    "type": "temperature",
                    "type_label": "Температура",
                    "measured_at": r["measured_at"],
                    "measured_at_ru": format_dt_ru(r["measured_at"]),
                    "display": f"{val:.1f} °C",
                    "display_html": f"{val:.1f} °C",
                    "display_pdf": f"{val:.1f} °C",
                    "comment": r["comment"] or "",
                    "sort_value": val,
                    "temperature_c": val,
                }
            )

    if "weight" in selected_types:
        rows = db.execute(
            """
            SELECT *
            FROM weight_entries
            WHERE user_id = ?
              AND deleted_at IS NULL
              AND date(measured_at) >= date(?)
              AND date(measured_at) <= date(?)
            ORDER BY measured_at DESC
            """,
            (user_id, d_from, d_to),
        ).fetchall()

        for r in rows:
            val = float(r["weight_kg"])
            entries.append(
                {
                    "id": r["id"],
                    "type": "weight",
                    "type_label": "Вес",
                    "measured_at": r["measured_at"],
                    "measured_at_ru": format_dt_ru(r["measured_at"]),
                    "display": f"{val:.1f} кг",
                    "display_html": f"{val:.1f} кг",
                    "display_pdf": f"{val:.1f} кг",
                    "comment": r["comment"] or "",
                    "sort_value": val,
                    "weight_kg": val,
                }
            )

    if sort == "value":
        entries.sort(key=lambda x: (x["type"], x["sort_value"]))
    else:
        entries.sort(key=lambda x: x["measured_at"], reverse=True)
    return entries, d_from, d_to


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


MONTHS_RU_SHORT = ["янв.", "февр.", "мар.", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]

def format_dt_ru(value):
    try:
        s = str(value)
        d = date.fromisoformat(s[:10])
        t = s[11:16]
        return f"{d.day:02d} {MONTHS_RU_SHORT[d.month - 1]} {d.year % 100:02d} г. {t}"
    except Exception:
        return str(value)

def build_pdf(entries, d_from, d_to, sort="date", filter_label="Все записи", owner_name="", ai_used=False):
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
    recommendation_style = ParagraphStyle(
        "Recommendation",
        fontName=FONT_NAME,
        fontSize=8,
        leading=10,
        spaceAfter=0,
    )

    elements = [
        Paragraph("Медицинский дневник", title_style),
        Paragraph(f"Период: {escape(d_from)} — {escape(d_to)}", normal_style),
        Paragraph(f"Пользователь: {escape(owner_name)}", normal_style),
        Paragraph(f"Фильтр: {escape(filter_label)}", normal_style),
        Paragraph(
            "Подсветка: жёлтый — ниже нормы, зелёный — норма, красный — выше нормы. "
            "Статистические нормы, не диагноз.",
            normal_style,
        ),
        Paragraph("Данные введены пользователем и не являются медицинским заключением.", normal_style),
        Paragraph(
            "Оценка и рекомендация: без * — сформированы ИИ GigaChat; с * — сформированы встроенной системой правил.",
            normal_style,
        ),
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

            # Один блок записи = две строки:
            # 1) все исходные данные;
            # 2) одна объединённая ячейка на всю ширину с оценкой и рекомендацией.
            data = [[
                Paragraph("Дата и время", header_style),
                Paragraph("Тип", header_style),
                Paragraph("Значение", header_style),
                Paragraph("Комментарий", header_style),
            ]]

            for e in day_entries:
                assessment_text = _normalize_ai_text(e.get("assessment"), 160)
                recommendation_text = _normalize_ai_text(e.get("recommendation"), 300)

                is_ai = e.get("assessment_source") == "ai"
                mark = "" if is_ai else "*"
                if assessment_text and recommendation_text:
                    assessment_html = (
                        "<b>Оценка" + mark + ":</b> " + escape(assessment_text)
                        + "<br/><b>Рекомендация" + mark + ":</b> " + escape(recommendation_text)
                    )
                elif assessment_text:
                    assessment_html = "<b>Оценка" + mark + ":</b> " + escape(assessment_text)
                elif recommendation_text:
                    assessment_html = "<b>Рекомендация" + mark + ":</b> " + escape(recommendation_text)
                else:
                    assessment_html = "Оценка отсутствует."

                data.append([
                    Paragraph(escape(e.get("measured_at_ru") or e["measured_at"]), cell_style),
                    Paragraph(escape(e["type_label"]), cell_style),
                    Paragraph(e.get("display_pdf") or escape(e["display"]), cell_style),
                    Paragraph(escape(e.get("comment") or "") or "-", cell_style),
                ])
                data.append([
                    Paragraph(assessment_html, recommendation_style),
                    "",
                    "",
                    "",
                ])

            table = Table(
                data,
                repeatRows=1,
                colWidths=[36 * mm, 27 * mm, 55 * mm, 68 * mm],
            )
            style_commands = [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]

            # Строки 2,4,6... являются объединёнными рекомендациями.
            for row_index in range(2, len(data), 2):
                style_commands.extend([
                    ("SPAN", (0, row_index), (-1, row_index)),
                    ("BACKGROUND", (0, row_index), (-1, row_index), colors.HexColor("#f7f7f7")),
                    ("TOPPADDING", (0, row_index), (-1, row_index), 3),
                    ("BOTTOMPADDING", (0, row_index), (-1, row_index), 3),
                ])

            table.setStyle(TableStyle(style_commands))
            elements.append(table)
            elements.append(Spacer(1, 4 * mm))

        elements.append(
            Paragraph(
                "* Оценка и рекомендация сформированы встроенной системой правил, а не ИИ.",
                normal_style,
            )
        )

    doc.build(elements)
    buffer.seek(0)
    return buffer


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
            if not (0 <= low < high <= 1000):
                return jsonify(error=f"Диапазон «{key}»: минимум должен быть меньше максимума (0–1000)"), 400
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
