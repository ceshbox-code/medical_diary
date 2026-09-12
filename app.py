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

    settings, ranges = get_user_settings(session["user_id"])
    return render_template_string(
        APP_HTML,
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
          el.textContent = friendlyErrorMessage(err);
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
<script>(function(){try{var t=localStorage.getItem('medical_diary_theme');var dark=t?(t==='dark'):(window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches);if(dark)document.documentElement.classList.add('theme-dark')}catch(e){}})();</script>
<title>Медицинский дневник</title>
<link rel="apple-touch-icon" sizes="180x180" href="/apple-icon-180x180.png">
<link rel="manifest" href="/manifest.json">
<style>
:root{--bg:#f4f7fb;--surface:#fff;--surface-2:#eef3f9;--surface-3:#e7edf5;--text:#182231;--muted:#708096;--faint:#9aa8b8;--border:#d7e0eb;--primary:#2f8cff;--primary-strong:#1876e8;--primary-soft:#e9f3ff;--success:#0da96b;--success-soft:#e7faf2;--warning:#d89a2b;--warning-soft:#fff5df;--danger:#e95d73;--danger-soft:#fff0f3;--glucose:#2f8cff;--vitals:#ef4f78;--food:#15b878;--shadow:0 8px 24px rgba(27,54,84,.08);--radius:18px}
html.theme-dark{--bg:#08111d;--surface:#101b2a;--surface-2:#142238;--surface-3:#1b2a40;--text:#f3f7fc;--muted:#a7b5c8;--faint:#7789a0;--border:#29405b;--primary:#2f94ff;--primary-strong:#55a8ff;--primary-soft:#102e50;--success:#20d58a;--success-soft:#0d3426;--warning:#f0b84d;--warning-soft:#392c14;--danger:#ff7187;--danger-soft:#3a1822;--shadow:0 12px 30px rgba(0,0,0,.28)}
*,:before,:after{box-sizing:border-box}html,body{min-height:100%;overflow-x:hidden}body{margin:0;background:radial-gradient(circle at 50% -10%,rgba(47,140,255,.08),transparent 35%),var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",sans-serif;-webkit-text-size-adjust:100%;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);overscroll-behavior-x:none}[hidden]{display:none!important}button,input,select,textarea{font:inherit}button{cursor:pointer}button:disabled{opacity:.55;cursor:wait}
.app-shell{width:100%;max-width:760px;margin:0 auto;padding-bottom:100px}.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:18px 18px 6px}.brand{min-width:0}.eyebrow{font-size:13px;font-weight:650;color:var(--muted);margin-bottom:3px}.brand-title{font-size:22px;line-height:1.1;font-weight:850;letter-spacing:-.5px}.link-btn{border:0;background:transparent;color:var(--primary-strong);font-size:13px;font-weight:800;padding:0}.user-pill{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:13px;font-weight:700}.header-action{width:42px;height:42px;border:1px solid var(--border);border-radius:14px;background:var(--surface);color:var(--text);font-size:20px;box-shadow:var(--shadow)}
main{width:100%;padding:10px 16px 18px}.page-intro{margin:8px 2px 18px}.page-intro h1,.history-head h1{margin:0;font-size:32px;line-height:1.05;letter-spacing:-.8px}.page-intro p{margin:7px 0 0;color:var(--muted);font-size:14px}.section-label{display:flex;align-items:center;justify-content:space-between;margin:18px 2px 10px}.section-label strong{font-size:14px;letter-spacing:.2px}.section-label span{font-size:13px;color:var(--primary-strong);font-weight:750}
.quick-list{display:grid;gap:10px}.quick-card{display:grid;grid-template-columns:64px minmax(0,1fr) 22px;align-items:center;column-gap:13px;width:100%;min-height:82px;padding:14px;border:1px solid var(--border);border-radius:17px;background:linear-gradient(145deg,var(--surface),var(--surface-2));color:var(--text);text-align:left;box-shadow:var(--shadow);-webkit-appearance:none;appearance:none}.quick-card:active{transform:scale(.99)}.quick-icon,.history-icon{width:48px;height:48px;display:grid;place-items:center;flex:0 0 48px;border-radius:50%;font-size:24px;color:var(--text);background:var(--bg);border:1px solid var(--border);box-shadow:none}.history-icon.glucose{color:var(--glucose)}.history-icon.vitals{color:var(--vitals)}.history-icon.food{color:var(--food)}.history-icon.temperature{color:#e28a2f}.history-icon.weight{color:#7a5cff}.quick-icon{width:64px;height:64px;flex:0 0 64px;font-size:48px}.quick-icon.glucose{color:var(--glucose)}.quick-icon.vitals{color:var(--vitals)}.quick-icon.food{color:var(--food)}.quick-icon.temperature{color:#e28a2f}.quick-icon.weight{color:#7a5cff}.quick-copy{min-width:0;display:flex;flex-direction:column;justify-content:center;align-items:flex-start}.quick-title{display:block;font-size:17px;line-height:1.2;font-weight:800;overflow-wrap:break-word;word-break:break-word;max-width:100%}.quick-sub{display:block;margin-top:6px;font-size:13px;line-height:1.2;color:var(--muted);font-weight:650;overflow-wrap:break-word;word-break:break-word;max-width:100%}.chevron{font-size:25px;line-height:1;color:var(--faint);justify-self:end}
.recent-list{display:grid;gap:8px}.recent-card{display:flex;align-items:center;gap:10px;padding:11px 13px;border:1px solid var(--border);border-radius:15px;background:var(--surface);box-shadow:0 4px 14px rgba(27,54,84,.05)}.recent-copy{min-width:0;flex:1}.recent-title{font-size:14px;font-weight:800}.recent-value{margin-top:2px;font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.recent-time{font-size:11px;color:var(--muted);align-self:flex-start}.day-summary{display:grid;grid-template-columns:repeat(3,1fr);margin-top:10px;border:1px solid var(--border);border-radius:16px;background:var(--surface);overflow:hidden}.day-stat{padding:12px 6px;text-align:center;border-right:1px solid var(--border);border-bottom:1px solid var(--border)}.day-stat:nth-child(3n){border-right:0}.day-stat:nth-last-child(-n+3){border-bottom:0}.day-stat .num{font-size:18px;font-weight:850}.day-stat .label{margin-top:2px;font-size:11px;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:12px;box-shadow:var(--shadow);overflow:hidden}.flat{box-shadow:none}label{display:block;margin:13px 0 6px;font-size:13px;font-weight:750;color:var(--text)}input,select,textarea{display:block;width:100%;min-height:48px;border:1px solid var(--border);border-radius:13px;background:var(--surface-2);color:var(--text);padding:10px 12px;font-size:17px;outline:none}input:focus,select:focus,textarea:focus{border-color:var(--primary);box-shadow:0 0 0 3px rgba(47,140,255,.13)}textarea{min-height:82px;resize:vertical}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}.row>div{min-width:0}
.admin-action{display:flex;align-items:center;justify-content:center;width:100%;min-height:48px;margin:10px 0 12px;padding:10px 14px;border:1px solid var(--primary);border-radius:13px;background:var(--primary);color:#fff;font-size:14px;font-weight:800;box-shadow:0 6px 16px rgba(47,140,255,.18)}.admin-action:active{transform:scale(.99)}.table-wrap{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid var(--border);border-radius:14px;background:var(--surface)}#users-table,#backup-table{width:100%;border-collapse:collapse;table-layout:fixed;min-width:520px}#users-table th,#users-table td,#backup-table th,#backup-table td{padding:11px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:middle;font-size:13px;line-height:1.25}#users-table th,#backup-table th{background:var(--surface-2);font-size:12px;font-weight:800;color:var(--muted);white-space:nowrap}#users-table tr:last-child td,#backup-table tr:last-child td{border-bottom:0}#users-table th:nth-child(1),#users-table td:nth-child(1){width:34%}#users-table th:nth-child(2),#users-table td:nth-child(2){width:42%}#users-table th:nth-child(3),#users-table td:nth-child(3){width:24%;text-align:right}.cell-actions{display:flex;justify-content:flex-end;align-items:center;gap:6px;white-space:nowrap}.cell-actions button{width:38px;height:38px;padding:0;border:1px solid var(--border);border-radius:11px;background:var(--surface-2);color:var(--text);display:inline-grid;place-items:center;font-size:16px}.cell-actions .del-btn{color:var(--danger)}#backup-table{min-width:620px}#backup-table th:nth-child(1),#backup-table td:nth-child(1){width:43%;word-break:break-word}#backup-table th:nth-child(2),#backup-table td:nth-child(2){width:22%;white-space:nowrap}#backup-table th:nth-child(3),#backup-table td:nth-child(3){width:13%;white-space:nowrap}#backup-table th:nth-child(4),#backup-table td:nth-child(4){width:22%;text-align:right;white-space:nowrap}.backup-restore-btn{min-height:38px;padding:8px 11px;border:1px solid var(--danger);border-radius:11px;background:var(--danger-soft);color:var(--danger);font-size:12px;font-weight:800}.admin-section details>summary{padding:2px 0 12px;font-weight:800}.admin-section form{margin-bottom:14px}.admin-section .table-wrap{margin-top:10px}.modal{position:fixed;inset:0;z-index:80;background:rgba(4,10,18,.62);display:flex;align-items:flex-end;justify-content:center;padding:0}.modal-panel{width:100%;max-width:760px;max-height:94vh;overflow:auto;background:var(--bg);border:1px solid var(--border);border-bottom:0;border-radius:24px 24px 0 0;padding:10px 16px calc(22px + env(safe-area-inset-bottom));box-shadow:0 -18px 50px rgba(0,0,0,.28)}.modal-grabber{width:42px;height:5px;border-radius:10px;background:var(--faint);opacity:.55;margin:2px auto 13px}.modal-head{display:flex;align-items:center;gap:11px;margin-bottom:13px}.modal-title{flex:1;min-width:0}.modal-title h2{margin:0;font-size:22px;letter-spacing:-.4px}.modal-title p{margin:3px 0 0;color:var(--muted);font-size:12px}.close-btn{width:40px;height:40px;border:1px solid var(--border);border-radius:13px;background:var(--surface);color:var(--text);font-size:22px}.metric-hero{padding:17px;border:1px solid var(--border);border-radius:18px;background:linear-gradient(145deg,var(--surface),var(--surface-2));margin-bottom:12px}.metric-hero label{margin:0 0 6px}.metric-input{display:flex;align-items:baseline;gap:10px}.metric-input input{border:0;background:transparent;padding:0;min-height:64px;height:64px;font-size:56px;font-weight:850;letter-spacing:-2px;line-height:1;box-shadow:none}.metric-unit{font-size:16px;font-weight:800}.metric-status{margin-top:10px}.segmented{display:grid;grid-template-columns:1fr 1fr;gap:7px}.segmented input{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px}.segmented label{display:flex;align-items:center;justify-content:center;min-height:46px;margin:0;padding:8px;border:1px solid var(--border);border-radius:13px;background:var(--surface-2);font-size:14px}.segmented input:checked+label{background:var(--primary);border-color:var(--primary);color:#fff}.field-hint{margin:5px 0 0;color:var(--muted);font-size:11px}.quick-repeat{width:100%;min-height:46px;margin-top:10px;border:1px solid var(--border);border-radius:13px;background:var(--surface-2);color:var(--primary-strong);font-weight:750}.primary-action{width:100%;min-height:52px;margin-top:12px;border:0;border-radius:14px;background:var(--primary);color:#fff;font-size:16px;font-weight:800;box-shadow:0 7px 18px rgba(47,140,255,.22)}
.vitals-entry{display:grid;grid-template-columns:1fr 1fr;gap:10px}.bp-field{padding:13px;border:1px solid var(--border);border-radius:16px;background:var(--surface-2)}.bp-field .bp-caption{font-size:12px;color:var(--muted);font-weight:700}.bp-field input{margin-top:4px;min-height:58px;padding:4px 0;border:0;background:transparent;font-size:40px;font-weight:850;letter-spacing:-1px;box-shadow:none}.pulse-field{margin-top:10px}
.food-line{padding:13px;border:1px solid var(--border);border-radius:15px;background:var(--surface-2)}
.history-head{display:flex;align-items:flex-end;justify-content:space-between;gap:10px;margin:8px 2px 15px}.history-count{margin-top:5px;color:var(--success);font-size:13px;font-weight:800}.filter-toggle{min-height:40px;padding:0 12px;border:1px solid var(--border);border-radius:12px;background:var(--surface);color:var(--text);font-weight:750}.history-filters{padding:12px}.filter-title{font-size:12px;font-weight:800;color:var(--muted);margin-bottom:8px}.date-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}.date-btn{position:relative;display:flex;align-items:center;gap:7px;min-height:43px;margin:0;padding:8px 10px;border:1px solid var(--border);border-radius:11px;background:var(--surface-2);font-size:13px;font-weight:700;overflow:hidden}.date-btn input{position:absolute;inset:0;opacity:0;min-height:0;margin:0}.date-val{color:var(--primary-strong);white-space:nowrap}.preset-row{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-top:8px}.preset-btn{min-height:38px;border:1px solid var(--border);border-radius:10px;background:var(--surface);color:var(--text);font-size:12px;font-weight:800}.preset-btn.active{background:var(--primary);border-color:var(--primary);color:#fff}.history-toolbar-row{display:grid;grid-template-columns:1fr 1fr;gap:9px;margin-top:9px}.history-select label{margin:0 0 5px}.button-group{display:grid;grid-template-columns:1fr 1fr;gap:7px}.button-group input{position:absolute;opacity:0;width:1px;height:1px}.button-group label{display:flex;align-items:center;justify-content:center;min-height:42px;margin:0;border:1px solid var(--border);border-radius:11px;background:var(--surface);font-size:12px;font-weight:750;text-align:center}.button-group input:checked+label{background:var(--primary-soft);border-color:var(--primary);color:var(--primary-strong)}.export-row{margin-top:9px}.export-row .button{margin:0}.button{display:block;width:100%;min-height:46px;border:0;border-radius:13px;background:var(--primary);color:#fff;font-size:14px;font-weight:800}.legend{margin-bottom:10px;color:var(--muted);font-size:11px;line-height:1.4}.trend-chart-wrap{padding:13px;margin:10px 0;border:1px solid var(--border);border-radius:16px;background:var(--surface-2)}.trend-chart-title{font-size:13px;font-weight:800;margin-bottom:6px}.trend-chart-wrap canvas{display:block;width:100%;height:150px}.chart-legend{margin:4px 0 0;color:var(--muted);font-size:10px}.history-cards{display:grid;gap:7px}.history-day-title{display:flex;align-items:center;gap:7px;margin:12px 0 1px;font-size:17px;font-weight:850;letter-spacing:-.2px}.day-chip{padding:3px 8px;border-radius:999px;background:var(--surface-3);color:var(--muted);font-size:10px;font-weight:750}.history-entry{position:relative;min-width:0;padding:10px 52px 10px 11px;border:1px solid var(--border);border-radius:14px;background:linear-gradient(145deg,var(--surface),var(--surface-2));box-shadow:var(--shadow);overflow:hidden}.history-entry-top{display:flex;align-items:center;gap:8px;min-width:0}.history-entry-main{min-width:0;flex:1}.history-entry-title{font-size:14px;font-weight:850;line-height:1.15}.history-entry-meta{margin-top:2px;color:var(--muted);font-size:11px;white-space:nowrap}.history-value{display:flex;align-items:baseline;gap:7px;margin:8px 0 5px;min-width:0}.history-value .number{font-size:31px;font-weight:900;letter-spacing:-1px}.history-value .unit{font-size:13px;font-weight:800}.status-badge{display:inline-flex;align-items:center;gap:3px;padding:4px 7px;border:1px solid;border-radius:999px;font-size:10px;font-weight:800;line-height:1.1;white-space:nowrap}.status-ok{color:var(--success);border-color:var(--success);background:var(--success-soft)}.status-low,.status-high{color:var(--danger);border-color:var(--danger);background:var(--danger-soft)}.comment-line{margin-top:6px;padding-top:6px;border-top:1px solid var(--border);color:var(--muted);font-size:11px;line-height:1.3;overflow-wrap:anywhere}.history-actions{position:absolute;top:10px;right:8px;display:flex;flex-direction:column;gap:5px}.icon-btn{width:34px;height:34px;border:1px solid var(--border);border-radius:10px;background:var(--surface-2);color:var(--text);font-size:15px;box-shadow:none}.icon-btn.delete{border-color:rgba(233,93,115,.6);color:var(--danger);background:var(--danger-soft)}.vitals-values{display:grid;grid-template-columns:minmax(0,1.25fr) 1px minmax(0,.75fr);gap:9px;align-items:center;margin-top:8px}.vitals-divider{width:1px;height:58px;background:var(--border)}.vital-label{color:var(--muted);font-size:12px;font-weight:750}.pressure-number{margin-top:2px;font-size:27px;font-weight:900;letter-spacing:-.8px;white-space:nowrap}.pressure-unit,.pulse-unit{margin-top:1px;color:var(--muted);font-size:11px;font-weight:650}.vital-statuses{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px;margin-top:7px}.vital-status-item{min-width:0}.vital-status-item .status-badge{max-width:100%;overflow:hidden}.vital-caption{margin-top:2px;color:var(--muted);font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.edit-title{font-size:18px;font-weight:850;margin-bottom:8px}.message{margin-top:8px;font-size:13px;line-height:1.35}.ok{color:var(--success)}.error{color:var(--danger)}
.switch-row{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:50px;margin:5px 0;font-size:14px;font-weight:700}.switch-row input{position:absolute;opacity:0;width:1px;height:1px}.switch{width:50px;height:30px;border-radius:15px;background:var(--surface-3);border:1px solid var(--border);position:relative;flex:0 0 auto}.switch:after{content:'';position:absolute;top:2px;left:2px;width:24px;height:24px;border-radius:50%;background:#fff;box-shadow:0 2px 5px rgba(0,0,0,.2);transition:left .18s}.switch-row input:checked+.switch{background:var(--primary);border-color:var(--primary)}.switch-row input:checked+.switch:after{left:22px}.settings-section{margin-top:16px;padding-top:15px;border-top:1px solid var(--border)}.settings-title{font-size:15px;font-weight:850;margin-bottom:7px}.muted{color:var(--muted);font-size:13px;line-height:1.45}.about-note{font-size:13px;line-height:1.5}.about-note h3{font-size:15px;margin:16px 0 5px}.about-note p,.about-note li{margin:6px 0}.about-note ul,.about-note ol{padding-left:20px}.range-row{display:grid;grid-template-columns:minmax(0,1fr) 70px 70px;gap:7px;align-items:center;margin:7px 0}.range-row span{font-size:12px}.range-row input{min-height:40px;padding:5px 6px;font-size:13px}
.tab-bar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;gap:8px;padding:8px 12px calc(8px + env(safe-area-inset-bottom));background:var(--surface);border-top:1px solid var(--border);box-shadow:0 -8px 25px rgba(0,0,0,.12);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px)}.tab-btn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;min-height:56px;border:1px solid transparent;border-radius:17px;background:transparent;color:var(--muted);font-size:12px;font-weight:800}.tab-btn .tab-icon{font-size:21px;line-height:1}.tab-btn.active{background:var(--primary-soft);border-color:var(--primary);color:var(--primary-strong)}
#toast{position:fixed;left:12px;right:12px;z-index:200;top:calc(env(safe-area-inset-top) + 9px);display:flex;justify-content:center;pointer-events:none}#toast .toast-bubble{max-width:94%;padding:10px 14px;border-radius:13px;font-size:13px;font-weight:800;color:#fff;text-align:center;opacity:0;transform:translateY(-10px);transition:.2s}.toast-bubble.show{opacity:1!important;transform:translateY(0)!important}.toast-bubble.ok{background:var(--success)}.toast-bubble.error{background:var(--danger)}
#pdf-overlay{position:fixed;inset:0;z-index:100;background:#525659;display:flex;flex-direction:column;padding:env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)}.pdf-toolbar{display:flex;align-items:center;gap:7px;background:#fff;padding:8px 10px}.pdf-title{flex:1;min-width:0;font-weight:700;font-size:16px}.pdf-btn{width:auto;min-height:42px;line-height:42px;margin:0;padding:0 10px;font-size:18px;border-radius:10px;background:#f0f2f5;color:#17202b}.pdf-pages{flex:1}.pdf-status{color:#fff;text-align:center;padding:24px;font-size:16px}#zoom-label{min-width:48px;text-align:center;font-weight:700;font-size:14px;color:#333}#pdf-pages{flex:1;overflow:auto;-webkit-overflow-scrolling:touch;padding:10px}#pdf-pages canvas{display:block;margin:0 auto 10px;background:#fff;border-radius:3px;box-shadow:0 1px 4px rgba(0,0,0,.4)}
@media(min-width:520px) and (max-width:699px){.quick-list{grid-template-columns:repeat(2,minmax(0,1fr))}.quick-card{min-height:100px}}@media(min-width:700px){main{padding-left:20px;padding-right:20px}.quick-list{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}.quick-card{min-height:116px;grid-template-columns:64px minmax(0,1fr) 18px;align-content:center;padding:14px}.quick-copy{align-self:center}.quick-title{font-size:16px}.quick-sub{font-size:12px}.history-cards{grid-template-columns:1fr 1fr}.history-day-title{grid-column:1/-1}}
@media(max-width:430px){.history-toolbar-row{grid-template-columns:1fr}.date-grid{grid-template-columns:1fr 1fr}.preset-row{grid-template-columns:repeat(4,1fr)}.vitals-values{gap:8px}.pressure-number{font-size:27px}.vital-statuses{gap:4px}.status-badge{font-size:10px;padding:5px 6px}.range-row{grid-template-columns:minmax(0,1fr) 62px 62px}.day-summary{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<div id="toast"><div class="toast-bubble" id="toast-bubble"></div></div>
<div class="app-shell">
<header class="topbar">
  <div class="brand"><div class="eyebrow">{{ display_name }}</div><div class="brand-title">Медицинский дневник</div></div>
  <button type="button" id="header-logout-core" class="header-action" onclick="logout()" aria-label="Выйти" title="Выйти">↪</button>
</header>
<main>
<section id="page-input" class="tab-page">
  <div class="page-intro"><h1>Здравствуйте</h1><p id="today-label">Сегодня</p></div>
  <div class="section-label"><strong>Быстрая запись</strong><span>Нажмите, чтобы добавить</span></div>
  <div class="quick-list">
    <button type="button" class="quick-card" id="card-glucose" onclick="openEntry('glucose')"><span class="quick-icon glucose">💧</span><span class="quick-copy"><span class="quick-title">Глюкоза</span><span class="quick-sub">Добавить измерение</span></span><span class="chevron">›</span></button>
    <button type="button" class="quick-card" id="card-vitals" onclick="openEntry('vitals')"><span class="quick-icon vitals">♥</span><span class="quick-copy"><span class="quick-title">Давление и пульс</span><span class="quick-sub">Добавить измерение</span></span><span class="chevron">›</span></button>
  <button type="button" class="quick-card" id="card-temperature" onclick="openEntry('temperature')"><span class="quick-icon temperature">🌡️</span><span class="quick-copy"><span class="quick-title">Температура</span><span class="quick-sub">Добавить измерение</span></span><span class="chevron">›</span></button>
  <button type="button" class="quick-card" id="card-weight" onclick="openEntry('weight')"><span class="quick-icon weight">⚖️</span><span class="quick-copy"><span class="quick-title">Вес</span><span class="quick-sub">Добавить измерение</span></span><span class="chevron">›</span></button>
    <button type="button" class="quick-card" id="card-food" onclick="openEntry('food')"><span class="quick-icon food">🍴</span><span class="quick-copy"><span class="quick-title">Питание</span><span class="quick-sub">Добавить запись</span></span><span class="chevron">›</span></button>
  </div>
  <div class="section-label"><strong>Последние записи</strong><button type="button" class="link-btn" onclick="showPage('history')">Все →</button></div>
  <div id="dashboard-recent" class="recent-list"><div class="muted">Загрузка…</div></div>
  <div class="section-label"><strong>Сегодня</strong></div>
  <div id="day-summary" class="day-summary"><div class="day-stat"><div class="num" id="sum-total">—</div><div class="label">записи</div></div><div class="day-stat"><div class="num" id="sum-glucose">—</div><div class="label">глюкоза</div></div><div class="day-stat"><div class="num" id="sum-vitals">—</div><div class="label">давление</div></div><div class="day-stat"><div class="num" id="sum-temperature">—</div><div class="label">температура</div></div><div class="day-stat"><div class="num" id="sum-weight">—</div><div class="label">вес</div></div><div class="day-stat"><div class="num" id="sum-food">—</div><div class="label">питание</div></div></div>
</section>

<section id="page-history" class="tab-page" hidden>
  <div class="history-head"><div><h1>История</h1><div class="history-count" id="history-count">Записей: —</div></div><button type="button" class="filter-toggle" onclick="document.getElementById('history-filter-card').hidden=!document.getElementById('history-filter-card').hidden">⚱ Фильтры</button></div>
  <div class="card flat history-filters" id="history-filter-card">
    <div class="filter-title">Период</div>
    <div class="date-grid"><span class="date-btn">📅 <span>с</span><span class="date-val" id="date_from_label"></span><input type="date" id="date_from" aria-label="Дата начала периода"></span><span class="date-btn">📅 <span>по</span><span class="date-val" id="date_to_label"></span><input type="date" id="date_to" aria-label="Дата конца периода"></span></div>
    <div class="preset-row"><button type="button" class="preset-btn" onclick="setDateRangePreset(7)">7 дней</button><button type="button" class="preset-btn" onclick="setDateRangePreset(30)">30 дней</button><button type="button" class="preset-btn" onclick="setDateRangePreset(90)">3 месяца</button><button type="button" class="preset-btn" onclick="setDateRangePreset(365)">Год</button></div>
    <div class="history-toolbar-row"><div class="history-select"><label for="history_type">Тип записей</label><select id="history_type"><option value="all">📋 Все записи</option><option value="glucose">🩸 Только глюкоза</option><option value="vitals">♥ Только давление и пульс</option><option value="temperature">🌡️ Только температура</option><option value="weight">⚖️ Только вес</option><option value="food">🍴 Только питание</option></select></div><div class="history-select"><label>Сортировка</label><div class="button-group"><input type="radio" id="hs_date" name="history_sort" value="date" checked><label for="hs_date">📅 По дате</label><input type="radio" id="hs_value" name="history_sort" value="value"><label for="hs_value">🔢 По значению</label></div></div></div>
    <div class="export-row"><button type="button" class="button" id="export-btn" onclick="openPdfTypeModal()">📄 Выгрузить PDF</button></div>
  </div>
  <div class="card flat"><div class="legend">● Натощак · ■ После еды · зелёный — целевой диапазон · красный — вне диапазона. Подсветка справочная и не является диагнозом.</div><div class="trend-chart-wrap" id="trend-glucose-wrap" hidden><div class="trend-chart-title">🩸 Глюкоза, ммоль/л</div><canvas id="trend-glucose"></canvas><p class="chart-legend">● натощак · ■ после еды · зелёным — целевой диапазон</p></div><div class="trend-chart-wrap" id="trend-vitals-wrap" hidden><div class="trend-chart-title">♥ Давление, мм рт. ст.</div><canvas id="trend-vitals"></canvas><p class="chart-legend">● систолическое · ■ диастолическое · зелёным — целевой диапазон</p></div><div class="trend-chart-wrap" id="trend-temperature-wrap" hidden><div class="trend-chart-title">🌡️ Температура, °C</div><canvas id="trend-temperature"></canvas><p class="chart-legend">● температура</p></div><div class="trend-chart-wrap" id="trend-weight-wrap" hidden><div class="trend-chart-title">⚖️ Вес, кг</div><canvas id="trend-weight"></canvas><p class="chart-legend">● вес</p></div><div class="message" id="history-msg"></div><div id="history-cards" class="history-cards" aria-live="polite"></div></div>
  <div class="card" id="edit-card" hidden><div class="edit-title" id="edit-title">✏️ Редактирование</div><div id="edit-glucose" hidden><label>Тип измерения</label><select id="edit_glucose_type"><option value="fasting">🌅 Натощак</option><option value="post_meal">🍽️ После еды</option></select><label>Значение, ммоль/л</label><input id="edit_glucose_value" class="decimal-input" type="text" inputmode="decimal"></div><div id="edit-vitals" hidden><div class="row"><div><label>Систолическое</label><input id="edit_systolic" type="number" min="30" max="400" inputmode="numeric"></div><div><label>Диастолическое</label><input id="edit_diastolic" type="number" min="10" max="300" inputmode="numeric"></div></div><label>Пульс</label><input id="edit_pulse" type="number" min="20" max="300" inputmode="numeric"></div><div id="edit-temperature" hidden><label>Температура, °C</label><input id="edit_temperature_value" class="decimal-input" type="text" inputmode="decimal"></div><div id="edit-weight" hidden><label>Вес, кг</label><input id="edit_weight_value" class="decimal-input" type="text" inputmode="decimal"></div><div id="edit-food" hidden><label>Продукт</label><input id="edit_food_name" maxlength="150"><div class="row"><div><label>Количество</label><input id="edit_amount_value" class="decimal-input" type="text" inputmode="decimal"></div><div><label>Единица</label><select id="edit_amount_unit"><option value="г">г</option><option value="мл">мл</option><option value="шт">шт</option><option value="порция">порция</option></select></div></div></div><label>Дата и время</label><input id="edit_measured_at" type="datetime-local"><label>Комментарий</label><textarea id="edit_comment" maxlength="1000"></textarea><button type="button" class="primary-action" onclick="saveEdit()">Сохранить изменения</button><button type="button" class="quick-repeat" onclick="closeEdit()">Отмена</button><div class="message" id="edit-msg"></div></div>
</section>
    <section id="page-settings" class="tab-page" hidden>
      <div class="page-intro"><div><h1>Ещё</h1><p>Настройки, помощь и управление данными.</p></div></div>
      <div class="card"><details><summary>ℹ️ О программе и помощь</summary><div class="about-note">
        <p><strong>Медицинский дневник</strong> — сервис для хранения и наблюдения показателей: глюкоза, давление, пульс, температура, вес и питание.</p><p><strong>Важно:</strong> приложение не ставит диагнозы и не назначает лечение. Автоматические оценки — как встроенные, так и с использованием ИИ — носят справочный характер.</p>
        <h3>Как пользоваться</h3><ol><li>Войдите в приложение.</li><li>Добавьте измерение.</li><li>Проверьте записи в «Истории».</li><li>При необходимости отредактируйте или удалите запись.</li><li>Выгрузите PDF для врача.</li></ol>
        <h3>Как вводить данные</h3><ul><li>Глюкоза: выберите тип — натощак или после еды, укажите значение в ммоль/л.</li><li>Давление: укажите систолическое и диастолическое значение, пульс — при наличии.</li><li>Температура: укажите значение в °C.</li><li>Вес: укажите значение в кг.</li><li>Питание: продукт, количество и единицу измерения.</li><li>В комментарии полезно указывать самочувствие, еду, нагрузку и другие факторы.</li></ul>
        <h3>История и фильтры</h3><ul><li>В «Истории» можно отфильтровать записи по типу и периоду; для быстрого выбора периода используйте кнопки «7 дней», «30 дней», «3 месяца», «Год», либо укажите даты вручную.</li><li>Графики динамики по каждому показателю появляются автоматически, если за выбранный период есть минимум 2 записи этого типа.</li></ul>
        <h3>PDF-отчёт и оценка ИИ</h3><ul><li>PDF-отчёт формируется за выбранный период и тип записей — удобно взять с собой на приём к врачу.</li><li>При включённой опции «Использовать ИИ в оценках PDF» (раздел «Настройки») часть кратких комментариев к записям формируется нейросетью GigaChat на основе значения и справочных диапазонов. Такие формулировки в PDF отмечены отдельно от встроенных и, как и все автоматические оценки, не содержат диагнозов и назначений.</li><li>Опцию можно отключить в любой момент — тогда оценки в PDF будут формироваться только встроенными правилами, без обращения к ИИ.</li></ul>
        <h3>Безопасность</h3><ul><li>Не сообщайте пароль другим людям.</li><li>На чужом устройстве выходите из приложения.</li><li>Регулярно сохраняйте PDF и резервные копии базы данных.</li></ul>
      </div></details></div>
      <div class="card"><details><summary>⚙️ Настройки</summary>
        <div class="settings-section"><div class="settings-title">Оформление</div><label class="switch-row"><span>🌙 Тёмная тема</span><input type="checkbox" id="theme-toggle" onchange="onThemeToggleChange(this)"><span class="switch"></span></label></div>
        <div class="settings-section"><div class="settings-title">🤖 ИИ-оценка</div><label class="switch-row"><span>Использовать ИИ в оценках PDF</span><input type="checkbox" id="set-ai-enabled" checked><span class="switch"></span></label><p class="muted" id="ai-status">Проверка доступности…</p><button type="button" class="quick-repeat" onclick="testAi()">🔎 Проверить работу ИИ</button><div class="message" id="ai-msg"></div></div>
        <div class="settings-section"><div class="settings-title">Блоки на главной странице</div><label class="switch-row"><span>🩸 Глюкоза</span><input type="checkbox" id="set-glucose" checked><span class="switch"></span></label><label class="switch-row"><span>💓 Давление и пульс</span><input type="checkbox" id="set-vitals" checked><span class="switch"></span></label><label class="switch-row"><span>🌡️ Температура</span><input type="checkbox" id="set-temperature" checked><span class="switch"></span></label><label class="switch-row"><span>⚖️ Вес</span><input type="checkbox" id="set-weight" checked><span class="switch"></span></label><label class="switch-row"><span>🥗 Питание</span><input type="checkbox" id="set-food" checked><span class="switch"></span></label><div class="message" id="settings-msg"></div></div>
        <div class="settings-section"><div class="settings-title" id="wa-summary">🔐 Биометрия</div><p class="muted" id="wa-hint">Быстрый вход по биометрии этого устройства, без пароля.</p><label class="switch-row"><span id="wa-toggle-label">Вход по биометрии</span><input type="checkbox" id="wa-toggle" onchange="onWaToggleChange(this)"><span class="switch"></span></label><p class="muted" id="wa-availability"></p><div class="message" id="wa-msg"></div></div>
        <div class="settings-section"><div class="settings-title">Персональные диапазоны нормы</div><p class="muted">Используются только для справочной подсветки. Если врач указал другие целевые значения, впишите их здесь.</p><label class="switch-row"><span>Использовать значения по умолчанию</span><input type="checkbox" id="ranges-default-toggle" onchange="onRangesDefaultToggleChange(this)"><span class="switch"></span></label><div id="range-inputs">
          <div class="range-row"><span>Глюкоза натощак, ммоль/л</span><input class="decimal-input" type="text" inputmode="decimal" id="range-glucose_fasting-low" oninput="scheduleSaveRanges()"><input class="decimal-input" type="text" inputmode="decimal" id="range-glucose_fasting-high" oninput="scheduleSaveRanges()"></div>
          <div class="range-row"><span>Глюкоза после еды, ммоль/л</span><input class="decimal-input" type="text" inputmode="decimal" id="range-glucose_post-low" oninput="scheduleSaveRanges()"><input class="decimal-input" type="text" inputmode="decimal" id="range-glucose_post-high" oninput="scheduleSaveRanges()"></div>
          <div class="range-row"><span>Систолическое, мм рт. ст.</span><input type="number" step="1" id="range-systolic-low" oninput="scheduleSaveRanges()"><input type="number" step="1" id="range-systolic-high" oninput="scheduleSaveRanges()"></div>
          <div class="range-row"><span>Диастолическое, мм рт. ст.</span><input type="number" step="1" id="range-diastolic-low" oninput="scheduleSaveRanges()"><input type="number" step="1" id="range-diastolic-high" oninput="scheduleSaveRanges()"></div>
          <div class="range-row"><span>Пульс, уд/мин</span><input type="number" step="1" id="range-pulse-low" oninput="scheduleSaveRanges()"><input type="number" step="1" id="range-pulse-high" oninput="scheduleSaveRanges()"></div>
        </div><div class="message" id="ranges-msg"></div></div>
      </details></div>

      {% if is_admin %}
      <div class="card admin-section"><details><summary>👥 Пользователи</summary><form id="user-form"><label>Логин</label><input name="username" maxlength="64" autocomplete="off" required><label>Отображаемое имя</label><input name="display_name" maxlength="100" required><label>Пароль</label><input name="password" type="password" minlength="8" autocomplete="new-password" required><button type="submit" class="admin-action">Добавить пользователя</button><div class="message" id="user-msg"></div></form><form id="user-edit-form" hidden><div class="edit-title" id="user-edit-title">✏️ Редактирование пользователя</div><label>Логин</label><input name="username" maxlength="64" autocomplete="off" required><label>Отображаемое имя</label><input name="display_name" maxlength="100" required><label>Новый пароль</label><input name="password" type="password" minlength="8" autocomplete="new-password"><button type="submit">Сохранить</button><button type="button" class="secondary" onclick="cancelUserEdit()">Отмена</button><div class="message" id="user-edit-msg"></div></form><div class="table-wrap"><table id="users-table"><thead><tr><th>Имя</th><th>Логин</th><th></th></tr></thead><tbody></tbody></table></div></details></div>
      <div class="card admin-section"><details><summary>🗄️ Резервные копии БД</summary><p class="muted" id="backup-status">Загрузка статуса…</p><button type="button" class="admin-action" onclick="runBackupNow()">Снять копию сейчас</button><div class="message" id="backup-msg"></div><div class="table-wrap"><table id="backup-table"><thead><tr><th>Файл</th><th>Создан</th><th>Размер</th><th>Действие</th></tr></thead><tbody></tbody></table></div></details></div>
      {% endif %}
    </section>
</main></div>
<nav class="tab-bar" aria-label="Основная навигация"><button type="button" class="tab-btn" id="tab-btn-input" onclick="showPage('input')"><span class="tab-icon">📝</span><span>Ввод</span></button><button type="button" class="tab-btn" id="tab-btn-history" onclick="showPage('history')"><span class="tab-icon">▥</span><span>История</span></button><button type="button" class="tab-btn" id="tab-btn-settings" onclick="showPage('settings')"><span class="tab-icon">⚙️</span><span>Ещё</span></button></nav>
<div id="entry-modal" class="modal" hidden><div class="modal-panel"><div class="modal-grabber"></div><div class="modal-head"><div id="modal-icon" class="quick-icon glucose">💧</div><div class="modal-title"><h2 id="modal-title">Глюкоза</h2><p id="modal-subtitle">Ввод показателя</p></div><button type="button" id="modal-close-core" class="close-btn" onclick="closeEntry()">×</button></div>
<form id="glucose-form" hidden><div class="metric-hero"><label>Значение, ммоль/л</label><div class="metric-input"><input name="value" class="decimal-input" type="text" inputmode="decimal" pattern="[0-9]+([.,][0-9]+)?" required><span class="metric-unit">ммоль/л</span></div><div class="metric-status"><p class="field-hint" id="hint-glucose"></p></div></div><label>Контекст измерения</label><div class="segmented"><input type="radio" id="gt_fasting" name="glucose_type" value="fasting" checked><label for="gt_fasting">🌅 Натощак</label><input type="radio" id="gt_postmeal" name="glucose_type" value="post_meal"><label for="gt_postmeal">🍽️ После еды</label></div><label>Дата и время</label><input name="measured_at" type="datetime-local" class="dt"><label>Комментарий <span class="muted">(необязательно)</span></label><textarea name="comment" maxlength="1000" placeholder="Например: хорошее самочувствие"></textarea><button type="button" class="quick-repeat" onclick="repeatLast('glucose', event)">↻ Повторить последнее</button><p class="last-entry-status muted" id="last-status-glucose"></p><button type="submit" class="primary-action">Сохранить измерение</button><div class="message" id="glucose-msg"></div></form>
<form id="vitals-form" hidden><div class="vitals-entry"><div class="bp-field"><div class="bp-caption">Систолическое · мм рт. ст.</div><input name="systolic" type="number" min="30" max="400" inputmode="numeric" required><p class="field-hint" id="hint-systolic"></p></div><div class="bp-field"><div class="bp-caption">Диастолическое · мм рт. ст.</div><input name="diastolic" type="number" min="10" max="300" inputmode="numeric" required><p class="field-hint" id="hint-diastolic"></p></div></div><div class="bp-field pulse-field"><div class="bp-caption">Пульс · уд/мин</div><input name="pulse" type="number" min="20" max="300" inputmode="numeric"><p class="field-hint" id="hint-pulse"></p></div><label>Дата и время</label><input name="measured_at" type="datetime-local" class="dt"><label>Комментарий <span class="muted">(необязательно)</span></label><textarea name="comment" maxlength="1000" placeholder="Например: после прогулки"></textarea><button type="button" class="quick-repeat" onclick="repeatLast('vitals', event)">↻ Повторить последнее</button><p class="last-entry-status muted" id="last-status-vitals"></p><button type="submit" class="primary-action">Сохранить измерение</button><div class="message" id="vitals-msg"></div></form>
<form id="temperature-form" hidden><div class="metric-hero temperature-hero"><label>Температура, °C</label><div class="metric-input"><input name="value" class="decimal-input" type="text" inputmode="decimal" pattern="[0-9]+([.,][0-9]+)?" required><span class="metric-unit">°C</span></div></div><label>Дата и время</label><input name="measured_at" type="datetime-local" class="dt"><label>Комментарий <span class="muted">(необязательно)</span></label><textarea name="comment" maxlength="1000" placeholder="Например: после пробуждения"></textarea><button type="button" class="quick-repeat" onclick="repeatLast('temperature', event)">↻ Повторить последнее</button><p class="last-entry-status muted" id="last-status-temperature"></p><button type="submit" class="primary-action">Сохранить измерение</button><div class="message" id="temperature-msg"></div></form>
<form id="weight-form" hidden><div class="metric-hero weight-hero"><label>Вес, кг</label><div class="metric-input"><input name="value" class="decimal-input" type="text" inputmode="decimal" pattern="[0-9]+([.,][0-9]+)?" required><span class="metric-unit">кг</span></div></div><label>Дата и время</label><input name="measured_at" type="datetime-local" class="dt"><label>Комментарий <span class="muted">(необязательно)</span></label><textarea name="comment" maxlength="1000" placeholder="Например: утром натощак"></textarea><button type="button" class="quick-repeat" onclick="repeatLast('weight', event)">↻ Повторить последнее</button><p class="last-entry-status muted" id="last-status-weight"></p><button type="submit" class="primary-action">Сохранить измерение</button><div class="message" id="weight-msg"></div></form>
<form id="food-form" hidden><div class="food-line"><label style="margin-top:0">Продукт</label><input name="food_name" maxlength="150" required placeholder="Например: овсяная каша"><div class="row"><div><label>Количество</label><input name="amount_value" class="decimal-input" type="text" inputmode="decimal" required></div><div><label>Единица</label><select name="amount_unit"><option value="г">г</option><option value="мл">мл</option><option value="шт">шт</option><option value="порция">порция</option></select></div></div></div><label>Дата и время</label><input name="consumed_at" type="datetime-local" class="dt"><label>Комментарий <span class="muted">(необязательно)</span></label><textarea name="comment" maxlength="1000"></textarea><button type="button" class="quick-repeat" onclick="repeatLast('food', event)">↻ Повторить последнее</button><p class="last-entry-status muted" id="last-status-food"></p><button type="submit" class="primary-action">Сохранить запись</button><div class="message" id="food-msg"></div></form>
</div></div>
<div id="pdf-overlay" hidden><div class="pdf-toolbar"><button type="button" class="pdf-btn" onclick="zoomPdf(-1)">➖</button><span id="zoom-label">100%</span><button type="button" class="pdf-btn" onclick="zoomPdf(1)">➕</button><span class="pdf-title">📄 Медицинский дневник</span><button type="button" class="pdf-btn" onclick="downloadPdf()">⬇️</button><button type="button" class="pdf-btn" onclick="sharePdf()">📤</button><button type="button" class="pdf-btn" onclick="closePdfViewer()">❌</button></div><div id="pdf-pages"></div></div>
<div id="pdf-type-modal" class="modal" hidden><div class="modal-panel"><div class="modal-grabber"></div><div class="modal-head"><div class="modal-title"><h2>Записи для PDF</h2><p class="muted">Отметьте один или несколько типов</p></div><button type="button" class="close-btn" onclick="closePdfTypeModal()">×</button></div>
<div class="pdf-type-list">
<label class="switch-row"><span>🩸 Глюкоза</span><input type="checkbox" class="pdf-type-cb" value="glucose"><span class="switch"></span></label>
<label class="switch-row"><span>♥ Давление и пульс</span><input type="checkbox" class="pdf-type-cb" value="vitals"><span class="switch"></span></label>
<label class="switch-row"><span>🌡️ Температура</span><input type="checkbox" class="pdf-type-cb" value="temperature"><span class="switch"></span></label>
<label class="switch-row"><span>⚖️ Вес</span><input type="checkbox" class="pdf-type-cb" value="weight"><span class="switch"></span></label>
<label class="switch-row"><span>🍴 Питание</span><input type="checkbox" class="pdf-type-cb" value="food"><span class="switch"></span></label>
</div>
<button type="button" class="primary-action" onclick="confirmPdfTypeSelection()">Сформировать PDF</button>
<p class="message" id="pdf-type-msg"></p>
</div></div>
<script>
(function () {
  // Минимальный аварийный слой интерфейса. Он не зависит от остального JS-бандла:
  // если дополнительный код упадёт, основные кнопки всё равно должны работать.
  function nowLocalDateTime() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function fallbackOpen(type) {
    var modal = document.getElementById('entry-modal');
    if (!modal) return;
    ['glucose','vitals','temperature','weight','food'].forEach(function(k) {
      var f = document.getElementById(k + '-form');
      if (f) f.hidden = (k !== type);
    });
    var cfg = {
      glucose:['Глюкоза','Ввод показателя','💧','glucose'],
      vitals:['Давление и пульс','Ввод показателей','♥','vitals'],
      temperature:['Температура','Ввод показателя','🌡️','temperature'],
      weight:['Вес','Ввод показателя','⚖️','weight'],
      food:['Питание','Новая запись','🍴','food']
    }[type];
    if (!cfg) return;
    var title=document.getElementById('modal-title'); if(title) title.textContent=cfg[0];
    var subtitle=document.getElementById('modal-subtitle'); if(subtitle) subtitle.textContent=cfg[1];
    var icon=document.getElementById('modal-icon'); if(icon){icon.textContent=cfg[2];icon.className='quick-icon '+cfg[3];}
    modal.hidden=false;
    document.body.style.overflow='hidden';
    var form=document.getElementById(type+'-form');
    if(form){var dt=form.querySelector('.dt');if(dt)dt.value=nowLocalDateTime();}
  }
  function fallbackPage(name) {
    ['input','history','settings'].forEach(function(p){
      var page=document.getElementById('page-'+p); if(page) page.hidden=(p!==name);
      var btn=document.getElementById('tab-btn-'+p); if(btn) btn.classList.toggle('active',p===name);
    });
  }
  function callOrFallback(fn, args, fallback) {
    try {
      if (typeof window[fn] === 'function') { window[fn].apply(window,args||[]); }
      else { fallback.apply(window,args||[]); }
    } catch (e) { fallback.apply(window,args||[]); }
  }
  function bind(id, handler) {
    var el=document.getElementById(id);
    if(!el || el.__coreBound) return;
    el.__coreBound=true;
    el.addEventListener('click', function(e){
      try {
        e.preventDefault();
        e.stopImmediatePropagation();
        handler(e);
      } catch (_) {}
    }, true);
  }
  bind('card-glucose', function(e){ callOrFallback('openEntry',['glucose'],fallbackOpen); });
  bind('card-vitals', function(e){ callOrFallback('openEntry',['vitals'],fallbackOpen); });
  bind('card-temperature', function(e){ callOrFallback('openEntry',['temperature'],fallbackOpen); });
  bind('card-weight', function(e){ callOrFallback('openEntry',['weight'],fallbackOpen); });
  bind('card-food', function(e){ callOrFallback('openEntry',['food'],fallbackOpen); });
  bind('tab-btn-input', function(e){ callOrFallback('showPage',['input'],fallbackPage); });
  bind('tab-btn-history', function(e){ callOrFallback('showPage',['history'],fallbackPage); });
  bind('tab-btn-settings', function(e){ callOrFallback('showPage',['settings'],fallbackPage); });
  bind('modal-close-core', function(e){ var m=document.getElementById('entry-modal');if(m){m.hidden=true;document.body.style.overflow='';} });
  bind('header-logout-core', function(e){
    try {
      if (typeof window.logout === 'function') { window.logout(); return; }
      var meta=document.querySelector('meta[name=csrf-token]');
      var token=meta ? meta.content : '';
      fetch('/logout',{method:'POST',headers:{'X-CSRF-Token':token}}).finally(function(){window.location='/login';});
    } catch (_) { window.location='/login'; }
  });
})();
</script>
<script>

function openEntry(type){
  var modal=document.getElementById('entry-modal'); if(!modal)return;
  ['glucose','vitals','temperature','weight','food'].forEach(function(k){var f=document.getElementById(k+'-form'); if(f)f.hidden=(k!==type);});
  var cfg={glucose:['Глюкоза','Ввод показателя','💧','glucose'],vitals:['Давление и пульс','Ввод показателей','♥','vitals'],temperature:['Температура','Ввод показателя','🌡️','temperature'],weight:['Вес','Ввод показателя','⚖️','weight'],food:['Питание','Новая запись','🍴','food']}[type];
  document.getElementById('modal-title').textContent=cfg[0]; document.getElementById('modal-subtitle').textContent=cfg[1];
  var icon=document.getElementById('modal-icon'); icon.textContent=cfg[2]; icon.className='quick-icon '+cfg[3];
  modal.hidden=false; document.body.style.overflow='hidden';
  var form=document.getElementById(type+'-form'); if(form){var dt=form.querySelector('.dt');if(dt)dt.value=localDateTime();}
  loadOneLastStatus(type);
}
function closeEntry(){var m=document.getElementById('entry-modal');if(m)m.hidden=true;document.body.style.overflow='';}
document.getElementById('entry-modal').addEventListener('click',function(e){if(e.target===this)closeEntry();});

// Десктопные Chrome/Opera открывают нативный календарь/таймпикер только по
// клику на маленькую иконку внутри поля, а не по клику в любом месте поля.
// Для date_from/date_to иконка визуально скрыта (поле растянуто прозрачным
// слоем поверх кастомной кнопки), поэтому клик по кнопке не всегда
// попадает в зону иконки и пикер не открывается. showPicker() открывает
// пикер программно по любому клику в пределах поля — работает во всех
// Chromium-браузерах (Chrome, Opera, Edge); там, где showPicker()
// недоступен (например, Firefox, Safari), просто ничего не делаем и
// оставляем обычное поведение браузера как было.
document.addEventListener('click', function(e) {
  var t = e.target;
  if (!t || t.tagName !== 'INPUT') return;
  if (t.type !== 'date' && t.type !== 'datetime-local' && t.type !== 'time') return;
  if (t.disabled || t.readOnly) return;
  if (typeof t.showPicker !== 'function') return;
  try { t.showPicker(); } catch (err) { /* пикер уже открыт или вызван не из пользовательского жеста — игнорируем */ }
}, true);

// Регистрация service worker для установки приложения на Android/iOS как
// PWA (иконка на экране, полноэкранный режим). Полностью необязательна:
// если /sw.js ещё не размещён на сервере или браузер не поддерживает
// Service Worker API — просто ничего не произойдёт, остальной функционал
// приложения не зависит от этого блока.
if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/sw.js').catch(function () {
      // Например, файл ещё не выложен в static/ — не мешаем работе приложения.
    });
  });
}

function todayHuman(){var d=new Date();var months=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];return d.getDate()+' '+months[d.getMonth()]+' '+d.getFullYear();}
document.getElementById('today-label').textContent='Сегодня, '+todayHuman();
function loadDashboard(){
  var box=document.getElementById('dashboard-recent'); if(!box)return;
  fetch('/api/history?date_from='+encodeURIComponent(localDate(-6))+'&date_to='+encodeURIComponent(localDate(0))+'&type=all&sort=date').then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}).then(function(out){
    var entries=out.entries||[]; box.innerHTML=''; entries.slice(0,3).forEach(function(e){var c=document.createElement('div');c.className='recent-card';var icon=document.createElement('div');icon.className='history-icon '+e.type;icon.textContent=e.type==='glucose'?'💧':e.type==='vitals'?'♥':e.type==='temperature'?'🌡️':e.type==='weight'?'⚖️':'🍴';c.appendChild(icon);var copy=document.createElement('div');copy.className='recent-copy';var t=document.createElement('div');t.className='recent-title';t.textContent=e.type==='glucose'?'Глюкоза':e.type==='vitals'?'Давление и пульс':e.type==='temperature'?'Температура':e.type==='weight'?'Вес':'Питание';copy.appendChild(t);var v=document.createElement('div');v.className='recent-value';if(e.type==='glucose')v.textContent=Number(e.value_mmol_l).toFixed(1)+' ммоль/л · '+(e.glucose_type==='fasting'?'натощак':'после еды');else if(e.type==='vitals')v.textContent=e.systolic_mmhg+' / '+e.diastolic_mmhg+' мм рт. ст. · пульс '+(e.pulse_bpm==null?'—':e.pulse_bpm);else if(e.type==='temperature')v.textContent=Number(e.temperature_c).toFixed(1)+' °C';else if(e.type==='weight')v.textContent=Number(e.weight_kg).toFixed(1)+' кг';else v.textContent=e.food_name+' · '+e.amount_value+' '+unitRu(e.amount_unit);copy.appendChild(v);c.appendChild(copy);var tm=document.createElement('div');tm.className='recent-time';tm.textContent=(e.measured_at||'').substring(11,16);c.appendChild(tm);box.appendChild(c);});if(!entries.length)box.innerHTML='<div class="muted">Пока нет записей</div>';
    var today=localDate(0), counts={glucose:0,vitals:0,temperature:0,weight:0,food:0}; entries.forEach(function(e){if((e.measured_at||'').substring(0,10)===today)counts[e.type]++;}); document.getElementById('sum-glucose').textContent=counts.glucose;document.getElementById('sum-vitals').textContent=counts.vitals;document.getElementById('sum-temperature').textContent=counts.temperature;document.getElementById('sum-weight').textContent=counts.weight;document.getElementById('sum-food').textContent=counts.food;document.getElementById('sum-total').textContent=counts.glucose+counts.vitals+counts.temperature+counts.weight+counts.food;
  }).catch(function(){box.innerHTML='<div class="muted">Не удалось загрузить последние записи</div>';});
}
loadDashboard();
    var csrf = document.querySelector('meta[name="csrf-token"]').content;
    var IS_ADMIN = {{ is_admin | tojson }};
    var USER_ID = {{ user_id }};
var USER_SETTINGS = {{ settings_json | safe }};
var RANGES = {{ ranges_json | safe }};
var DEFAULT_RANGES_JS = {{ default_ranges_json | safe }};

function applySettings(s) {
  var map = { glucose: 'card-glucose', vitals: 'card-vitals', temperature: 'card-temperature', weight: 'card-weight', food: 'card-food' };
  for (var k in map) {
    var el = document.getElementById(map[k]);
    if (el) { el.style.display = s[k] ? '' : 'none'; }
  }
  var cg = document.getElementById('set-glucose'); if (cg) { cg.checked = !!s.glucose; }
  var cv = document.getElementById('set-vitals'); if (cv) { cv.checked = !!s.vitals; }
  var ct = document.getElementById('set-temperature'); if (ct) { ct.checked = !!s.temperature; }
  var cw = document.getElementById('set-weight'); if (cw) { cw.checked = !!s.weight; }
  var cf = document.getElementById('set-food'); if (cf) { cf.checked = !!s.food; }
  var cai = document.getElementById('set-ai-enabled'); if (cai) { cai.checked = s.ai_enabled !== false; }
}

function bindSettings() {
  ['glucose', 'vitals', 'temperature', 'weight', 'food', 'ai_enabled'].forEach(function(k) {
    var el = document.getElementById('set-' + k);
    if (!el) { return; }
    el.addEventListener('change', function() {
      USER_SETTINGS[k] = el.checked;
      applySettings(USER_SETTINGS);
      sendJSON('POST', '/api/settings', USER_SETTINGS).then(function() {
        setMsg(k === 'ai_enabled' ? 'ai-msg' : 'settings-msg', 'Сохранено', true);
        if (k === 'ai_enabled') { updateAiStatus(); }
      }).catch(function(err) {
        setMsg(k === 'ai_enabled' ? 'ai-msg' : 'settings-msg', friendlyErrorMessage(err), false);
      });
    });
  });
}

applySettings(USER_SETTINGS || {});
bindSettings();

async function testAi() {
  var msg = document.getElementById('ai-msg');
  if (msg) { msg.textContent = '⏳ Выполняется реальный тест GigaChat…'; msg.className = 'message'; }
  try {
    var res = await fetch('/api/ai-test', {
      method: 'POST',
      headers: { 'X-CSRF-Token': csrf }
    });
    var out = await res.json();
    if (!res.ok || !out.ok) {
      throw new Error(out.message || ('HTTP ' + res.status));
    }
    if (msg) {
      msg.textContent = '🟢 GigaChat отвечает: ' + out.answer + ' · ' + (out.model || GIGACHAT_MODEL);
      msg.className = 'message success';
    }
    updateAiStatus();
  } catch (err) {
    if (msg) {
      msg.textContent = '🔴 ' + friendlyErrorMessage(err);
      msg.className = 'message error';
    }
  }
}

async function updateAiStatus() {
  var box = document.getElementById('ai-status');
  var toggle = document.getElementById('set-ai-enabled');
  if (!box) return;
  if (toggle && !toggle.checked) {
    box.textContent = '⚪ ИИ отключён пользователем';
    return;
  }
  box.textContent = '⏳ Проверка доступности ИИ…';
  try {
    var res = await fetch('/api/ai-status');
    var out = await res.json();
    if (out.available) {
      box.textContent = '🟢 ИИ: GigaChat доступен · ' + (out.model || 'GigaChat');
    } else {
      box.textContent = (out.code === 'disabled' || out.code === 'missing_key')
        ? '⚪ ИИ: ' + (out.message || 'не настроен')
        : '🔴 ИИ: ' + (out.message || 'недоступен');
    }
  } catch (err) {
    box.textContent = '🔴 ИИ: недоступен';
  }
}
updateAiStatus();

var THEME_KEY = 'medical_diary_theme';

function isThemeDark() {
  var stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
  if (stored) { return stored === 'dark'; }
  return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
}

function applyTheme() {
  var dark = isThemeDark();
  document.documentElement.classList.toggle('theme-dark', dark);
  var toggle = document.getElementById('theme-toggle');
  if (toggle) { toggle.checked = dark; }
}

function onThemeToggleChange(el) {
  try { localStorage.setItem(THEME_KEY, el.checked ? 'dark' : 'light'); } catch (e) {}
  applyTheme();
}

applyTheme();
if (window.matchMedia) {
  // Старые Safari/iOS используют addListener вместо addEventListener.
  var themeMedia = window.matchMedia('(prefers-color-scheme: dark)');
  var onThemeMediaChange = function() {
    var stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
    if (!stored) { applyTheme(); }
  };
  try {
    if (themeMedia.addEventListener) { themeMedia.addEventListener('change', onThemeMediaChange); }
    else if (themeMedia.addListener) { themeMedia.addListener(onThemeMediaChange); }
  } catch (e) {}
}

function showToast(text, ok) {
  var bubble = document.getElementById('toast-bubble');
  if (!bubble) return;
  bubble.textContent = text;
  bubble.className = 'toast-bubble show ' + (ok ? 'ok' : 'error');
  if (ok && navigator.vibrate) { try { navigator.vibrate(15); } catch (e) {} }
  clearTimeout(showToast._t);
  showToast._t = setTimeout(function() { bubble.className = 'toast-bubble'; }, 2200);
}

var TAB_PAGES = ['input', 'history', 'settings'];
function showPage(name) {
  TAB_PAGES.forEach(function(p) {
    var pageEl = document.getElementById('page-' + p);
    var btnEl = document.getElementById('tab-btn-' + p);
    if (pageEl) { pageEl.hidden = (p !== name); }
    if (btnEl) { btnEl.classList.toggle('active', p === name); btnEl.setAttribute('aria-current', p === name ? 'page' : 'false'); }
  });
  try { localStorage.setItem('medical_diary_active_tab', name); } catch (e) {}
  if (name === 'history') { loadHistory(); }
}
(function() {
  var saved = 'input';
  try { saved = localStorage.getItem('medical_diary_active_tab') || 'input'; } catch (e) {}
  if (TAB_PAGES.indexOf(saved) === -1) { saved = 'input'; }
  showPage(saved);
})();

function fmtRangeVal(n) {
  return (Math.round(n * 10) / 10).toString();
}

function updateGlucoseHint() {
  var el = document.getElementById('hint-glucose');
  if (!el) return;
  var checked = document.querySelector('input[name="glucose_type"]:checked');
  var key = (checked && checked.value === 'post_meal') ? 'glucose_post' : 'glucose_fasting';
  var r = RANGES[key];
  el.textContent = r ? ('Обычно ' + fmtRangeVal(r[0]) + '–' + fmtRangeVal(r[1]) + ' ммоль/л') : '';
}
document.querySelectorAll('input[name="glucose_type"]').forEach(function(el) {
  el.addEventListener('change', updateGlucoseHint);
});
updateGlucoseHint();

function updateVitalsHints() {
  var pairs = [['hint-systolic', 'systolic'], ['hint-diastolic', 'diastolic'], ['hint-pulse', 'pulse']];
  pairs.forEach(function(pair) {
    var el = document.getElementById(pair[0]);
    var r = RANGES[pair[1]];
    if (el && r) { el.textContent = 'Обычно ' + fmtRangeVal(r[0]) + '–' + fmtRangeVal(r[1]); }
  });
}
updateVitalsHints();

async function repeatLast(type, ev) {
  if (ev) { ev.preventDefault(); }
  try {
    var res = await fetch('/api/last?type=' + type);
    var out = await res.json();
    if (!res.ok) throw new Error(out.error || 'HTTP ' + res.status);
    if (!out.found) { showToast('Пока нет предыдущих записей этого типа', false); return; }
    if (type === 'glucose') {
      var radio = document.querySelector('input[name="glucose_type"][value="' + out.glucose_type + '"]');
      if (radio) { radio.checked = true; updateGlucoseHint(); }
      document.querySelector('#glucose-form [name="value"]').value = out.value;
      document.querySelector('#glucose-form [name="comment"]').value = out.comment || '';
    } else if (type === 'vitals') {
      document.querySelector('#vitals-form [name="systolic"]').value = out.systolic;
      document.querySelector('#vitals-form [name="diastolic"]').value = out.diastolic;
      document.querySelector('#vitals-form [name="pulse"]').value = out.pulse != null ? out.pulse : '';
      document.querySelector('#vitals-form [name="comment"]').value = out.comment || '';
    } else if (type === 'temperature') {
      document.querySelector('#temperature-form [name="value"]').value = out.value;
      document.querySelector('#temperature-form [name="comment"]').value = out.comment || '';
    } else if (type === 'weight') {
      document.querySelector('#weight-form [name="value"]').value = out.value;
      document.querySelector('#weight-form [name="comment"]').value = out.comment || '';
    } else {
      document.querySelector('#food-form [name="food_name"]').value = out.food_name;
      document.querySelector('#food-form [name="amount_value"]').value = out.amount_value;
      document.querySelector('#food-form [name="amount_unit"]').value = out.amount_unit;
      document.querySelector('#food-form [name="comment"]').value = out.comment || '';
    }
    showToast('Поля заполнены последней записью — проверьте перед сохранением', true);
  } catch (err) {
    showToast(friendlyErrorMessage(err), false);
  }
}


function fmtDateRuLong(day) {
  var months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  try { var d = new Date(day + 'T00:00:00'); return d.getDate() + ' ' + months[d.getMonth()] + ' ' + d.getFullYear() + ' г.'; } catch(e) { return day; }
}

function relativeDayLabel(measuredAt) {
  if (!measuredAt) { return ''; }
  var day = measuredAt.substring(0, 10);
  var time = measuredAt.substring(11, 16);
  var dayLabel;
  if (day === localDate(0)) { dayLabel = 'сегодня'; }
  else if (day === localDate(-1)) { dayLabel = 'вчера'; }
  else { dayLabel = fmtDateLabel(day); }
  return dayLabel + ', ' + time;
}

async function loadOneLastStatus(type) {
  var el = document.getElementById('last-status-' + type);
  if (!el) { return; }
  try {
    var res = await fetch('/api/last?type=' + type);
    var out = await res.json();
    if (!res.ok) { throw new Error(out.error || 'HTTP ' + res.status); }
    if (!out.found) { el.textContent = 'Записей ещё не было'; return; }

    var summary;
    if (type === 'glucose') {
      summary = out.value.toFixed(1) + ' ммоль/л (' + (out.glucose_type === 'fasting' ? 'натощак' : 'после еды') + ')';
    } else if (type === 'vitals') {
      summary = out.systolic + '/' + out.diastolic + (out.pulse != null ? ', пульс ' + out.pulse : '');
    } else if (type === 'temperature') {
      summary = Number(out.value).toFixed(1) + ' °C';
    } else if (type === 'weight') {
      summary = Number(out.value).toFixed(1) + ' кг';
    } else {
      summary = out.food_name + ', ' + out.amount_value + ' ' + out.amount_unit;
    }
    el.textContent = 'Последняя запись: ' + relativeDayLabel(out.measured_at) + ' — ' + summary;
  } catch (err) {
    el.textContent = '';
  }
}

function loadLastEntryStatuses() {
  loadOneLastStatus('glucose');
  loadOneLastStatus('vitals');
  loadOneLastStatus('temperature');
  loadOneLastStatus('weight');
  loadOneLastStatus('food');
}
loadLastEntryStatuses();

var RANGE_KEYS = ['glucose_fasting', 'glucose_post', 'systolic', 'diastolic', 'pulse'];

function setRangeInputsDisabled(disabled) {
  RANGE_KEYS.forEach(function(k) {
    var lowEl = document.getElementById('range-' + k + '-low');
    var highEl = document.getElementById('range-' + k + '-high');
    if (lowEl) { lowEl.disabled = disabled; }
    if (highEl) { highEl.disabled = disabled; }
  });
}

function fillRangeInputsFrom(rangesObj) {
  RANGE_KEYS.forEach(function(k) {
    var r = rangesObj[k];
    if (!r) { return; }
    var lowEl = document.getElementById('range-' + k + '-low');
    var highEl = document.getElementById('range-' + k + '-high');
    if (lowEl) { lowEl.value = r[0]; }
    if (highEl) { highEl.value = r[1]; }
  });
}

function populateRangeInputs() {
  fillRangeInputsFrom(RANGES);
  var toggle = document.getElementById('ranges-default-toggle');
  var usingDefault = USER_SETTINGS ? (USER_SETTINGS.ranges_default !== false) : true;
  if (toggle) { toggle.checked = usingDefault; }
  setRangeInputsDisabled(usingDefault);
}
populateRangeInputs();

// Поля для дробных чисел сделаны type="text" вместо type="number",
// потому что нативный number-инпут не принимает запятую как десятичный
// разделитель ни при какой локали — а это стандартный способ ввода
// дробей на русской клавиатуре. Здесь мягко приводим ввод к пригодному
// виду (не более одного разделителя, только цифры), а запятую сервер и
// так понимает (parse_float на бэкенде уже заменяет её на точку).
document.querySelectorAll('.decimal-input').forEach(function(el) {
  el.addEventListener('input', function() {
    var before = el.value;
    var cleaned = before.replace(/[^0-9.,]/g, '');
    var sepIndex = cleaned.search(/[.,]/);
    if (sepIndex !== -1) {
      cleaned = cleaned.slice(0, sepIndex + 1) + cleaned.slice(sepIndex + 1).replace(/[.,]/g, '');
    }
    if (cleaned !== before) {
      var pos = el.selectionStart - (before.length - cleaned.length);
      el.value = cleaned;
      try { el.setSelectionRange(Math.max(0, pos), Math.max(0, pos)); } catch (e) {}
    }
  });
});

function parseDecimal(str) {
  if (str == null) { return NaN; }
  return parseFloat(String(str).trim().replace(',', '.'));
}

function collectRangeInputs() {
  var ranges = {};
  for (var i = 0; i < RANGE_KEYS.length; i++) {
    var k = RANGE_KEYS[i];
    var low = parseDecimal(document.getElementById('range-' + k + '-low').value);
    var high = parseDecimal(document.getElementById('range-' + k + '-high').value);
    if (isNaN(low) || isNaN(high)) { return null; }
    ranges[k] = [low, high];
  }
  return ranges;
}

async function persistSettings(payload) {
  try {
    var out = await sendJSON('POST', '/api/settings', payload);
    if (out.ranges) {
      RANGES = out.ranges;
      updateGlucoseHint();
      updateVitalsHints();
    }
    if (out.settings && ('ranges_default' in out.settings)) {
      USER_SETTINGS.ranges_default = out.settings.ranges_default;
    }
    setMsg('ranges-msg', 'Сохранено', true);
    showToast('Сохранено', true);
  } catch (err) {
    setMsg('ranges-msg', friendlyErrorMessage(err), false);
    showToast(friendlyErrorMessage(err), false);
  }
}

var scheduleSaveRangesTimer = null;
function scheduleSaveRanges() {
  clearTimeout(scheduleSaveRangesTimer);
  scheduleSaveRangesTimer = setTimeout(function() {
    var ranges = collectRangeInputs();
    if (!ranges) {
      setMsg('ranges-msg', 'Заполните оба значения для всех диапазонов', false);
      return;
    }
    persistSettings({ ranges: ranges, ranges_default: false });
  }, 700);
}

function onRangesDefaultToggleChange(el) {
  if (el.checked) {
    // Не отправляем "ranges" вовсе — сохранённые персональные значения
    // на сервере остаются нетронутыми, просто временно не используются.
    fillRangeInputsFrom(DEFAULT_RANGES_JS);
    setRangeInputsDisabled(true);
    RANGES = JSON.parse(JSON.stringify(DEFAULT_RANGES_JS));
    updateGlucoseHint();
    updateVitalsHints();
    persistSettings({ ranges_default: true });
  } else {
    setRangeInputsDisabled(false);
    var ranges = collectRangeInputs();
    persistSettings({ ranges: ranges, ranges_default: false });
  }
}

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

    var MONTHS_RU_SHORT = ['янв.', 'февр.', 'мар.', 'апр.', 'мая', 'июня', 'июля', 'авг.', 'сент.', 'окт.', 'нояб.', 'дек.'];
function fmtDateTimeRu(v) {
  if (!v) return '';
  var d = v.substring(0, 10).split('-');
  var t = v.substring(11, 16);
  var m = parseInt(d[1], 10) - 1;
  return d[2] + ' ' + MONTHS_RU_SHORT[m] + ' ' + d[0].substring(2) + ' г. ' + t;
}

function updateDateLabels() {
      document.getElementById('date_from_label').textContent = fmtDateLabel(document.getElementById('date_from').value);
      document.getElementById('date_to_label').textContent = fmtDateLabel(document.getElementById('date_to').value);
    }

    var HISTORY_FILTERS_KEY = 'medical_diary_history_filters';

    function saveHistoryFilters() {
      try {
        localStorage.setItem(HISTORY_FILTERS_KEY, JSON.stringify({
          date_from: document.getElementById('date_from').value,
          date_to: document.getElementById('date_to').value,
          type: document.getElementById('history_type').value,
          sort: document.querySelector('input[name="history_sort"]:checked').value
        }));
      } catch (e) {}
    }

    function highlightActivePreset() {
      var df = document.getElementById('date_from').value;
      var dt = document.getElementById('date_to').value;
      var presetDays = [7, 30, 90, 365];
      document.querySelectorAll('.preset-btn').forEach(function(btn, i) {
        var days = presetDays[i];
        var matches = !!days && dt === localDate(0) && df === localDate(-days + 1);
        btn.classList.toggle('active', matches);
      });
    }

    function setDateRangePreset(days) {
      document.getElementById('date_from').value = localDate(-days + 1);
      document.getElementById('date_to').value = localDate(0);
      updateDateLabels();
      saveHistoryFilters();
      loadHistory();
      highlightActivePreset();
    }

    (function restoreHistoryFilters() {
      var saved = null;
      try { saved = JSON.parse(localStorage.getItem(HISTORY_FILTERS_KEY) || 'null'); } catch (e) {}

      document.querySelectorAll('.dt').forEach(function(el) { el.value = localDateTime(); });
      document.getElementById('date_from').value = (saved && saved.date_from) || localDate(-6);
      document.getElementById('date_to').value = (saved && saved.date_to) || localDate(0);
      if (saved && saved.type) { document.getElementById('history_type').value = saved.type; }
      if (saved && saved.sort) {
        var radio = document.querySelector('input[name="history_sort"][value="' + saved.sort + '"]');
        if (radio) { radio.checked = true; }
      }
      updateDateLabels();
      highlightActivePreset();
    })();

    document.getElementById('date_from').addEventListener('change', function() { updateDateLabels(); saveHistoryFilters(); loadHistory(); highlightActivePreset(); });
    document.getElementById('date_to').addEventListener('change', function() { updateDateLabels(); saveHistoryFilters(); loadHistory(); highlightActivePreset(); });
    document.getElementById('history_type').addEventListener('change', function() { saveHistoryFilters(); loadHistory(); });
    document.querySelectorAll('input[name="history_sort"]').forEach(function(el) {
      el.addEventListener('change', function() { saveHistoryFilters(); loadHistory(); });
    });

    function setMsg(id, text, ok) {
      var el = document.getElementById(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'message ' + (ok ? 'ok' : 'error');
    }

    function friendlyErrorMessage(err) {
      // fetch() отклоняет промис с TypeError при обрыве сети/офлайне —
      // технический текст вроде "Failed to fetch" пользователю ничего не
      // скажет, поэтому подменяем его на понятное сообщение.
      if (err instanceof TypeError || (err && /fetch/i.test(err.message || ''))) {
        return 'Нет соединения с сервером — проверьте интернет и попробуйте ещё раз.';
      }
      return (err && err.message) || 'Неизвестная ошибка';
    }

    async function postJSON(url, data, extraHeaders) {
      var res;
      try {
        res = await fetch(url, {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, extraHeaders || {}),
          body: JSON.stringify(data)
        });
      } catch (err) {
        throw new Error(friendlyErrorMessage(err));
      }

      if (res.status === 401) { window.location = '/login'; throw new Error('Требуется вход'); }

      var out = {};
      try { out = await res.json(); } catch (e) {}

      if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }
      return out;
    }

    function genIdemKey() {
      if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
      return 'idem-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    }

    document.getElementById('glucose-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/glucose', {
          glucose_type: f.glucose_type.value,
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('glucose-msg', '', true);
        showToast('Запись глюкозы сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('glucose-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('vitals-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/vitals', {
          systolic: f.systolic.value,
          diastolic: f.diastolic.value,
          pulse: f.pulse.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('vitals-msg', '', true);
        showToast('Запись давления/пульса сохранена', true);
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('vitals-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('temperature-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/temperature', {
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('temperature-msg', '', true);
        showToast('Запись температуры сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('temperature-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('weight-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/weight', {
          value: f.value.value,
          measured_at: f.measured_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('weight-msg', '', true);
        showToast('Запись веса сохранена', true);
        f.value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('weight-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    document.getElementById('food-form').addEventListener('submit', async function(e) {
      e.preventDefault();
      var f = e.target;
      var btn = f.querySelector('button[type="submit"]');
      if (btn) { btn.disabled = true; }
      try {
        await postJSON('/api/food', {
          food_name: f.food_name.value,
          amount_value: f.amount_value.value,
          amount_unit: f.amount_unit.value,
          consumed_at: f.consumed_at.value.replace('T', ' '),
          comment: f.comment.value
        }, { 'Idempotency-Key': genIdemKey() });
        setMsg('food-msg', '', true);
        showToast('Запись о питании сохранена', true);
        f.food_name.value = '';
        f.amount_value.value = '';
        f.comment.value = '';
        closeEntry();
        loadHistory();
        loadLastEntryStatuses();
      } catch (err) { setMsg('food-msg', friendlyErrorMessage(err), false); showToast(friendlyErrorMessage(err), false); }
      finally { if (btn) { btn.disabled = false; } }
    });

    function historyParams() {
      return {
        df: document.getElementById('date_from').value,
        dt: document.getElementById('date_to').value,
        type: document.getElementById('history_type').value,
        sort: document.querySelector('input[name="history_sort"]:checked').value
      };
    }

    function fmtDateRu(v) {
      if (!v) return '';
      var d = v.substring(0, 10).split('-');
      var m = parseInt(d[1], 10) - 1;
      return d[2] + ' ' + MONTHS_RU_SHORT[m] + ' ' + d[0].substring(2) + ' г.';
    }

    var STATUS_LINE_COLORS = { ok: '#007aff', low: '#ff3b30', high: '#ff3b30' };
    var BAND_COLOR_OUTER = 'rgba(180,235,150,0.40)';
    var BAND_COLOR_INNER = 'rgba(140,220,130,0.55)';

    function fmtDayTick(ts) {
      var d = new Date(ts);
      return pad(d.getDate()) + '.' + pad(d.getMonth() + 1);
    }

    function drawLineChart(canvas, series, bands) {
      if (!canvas) return;
      var dpr = window.devicePixelRatio || 1;
      var cssWidth = canvas.clientWidth || (canvas.parentElement && canvas.parentElement.clientWidth) || 300;
      var cssHeight = 150;
      canvas.width = cssWidth * dpr;
      canvas.height = cssHeight * dpr;
      canvas.style.height = cssHeight + 'px';
      var ctx = canvas.getContext('2d');
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cssWidth, cssHeight);

      var allPoints = [];
      series.forEach(function(s) { allPoints = allPoints.concat(s.points); });
      if (allPoints.length === 0) { return; }

      var xs = allPoints.map(function(p) { return p.x; });
      var ys = allPoints.map(function(p) { return p.y; });
      (bands || []).forEach(function(b) { ys.push(b.low); ys.push(b.high); });
      var minX = Math.min.apply(null, xs), maxX = Math.max.apply(null, xs);
      var minY = Math.min.apply(null, ys), maxY = Math.max.apply(null, ys);
      if (minX === maxX) { minX -= 1; maxX += 1; }
      if (minY === maxY) { minY -= 1; maxY += 1; }
      var padY = (maxY - minY) * 0.15;
      minY -= padY; maxY += padY;

      var spanDays = (maxX - minX) / 86400000;
      var padding = { left: 34, right: 8, top: 10, bottom: 18 };
      var plotW = cssWidth - padding.left - padding.right;
      var plotH = cssHeight - padding.top - padding.bottom;
      function px(x) { return padding.left + (x - minX) / (maxX - minX) * plotW; }
      function py(y) { return padding.top + (1 - (y - minY) / (maxY - minY)) * plotH; }

      (bands || []).forEach(function(b) {
        var yTop = py(b.high), yBot = py(b.low);
        ctx.fillStyle = b.color;
        ctx.fillRect(padding.left, yTop, plotW, Math.max(1, yBot - yTop));
      });

      ctx.strokeStyle = '#e5e5ea';
      ctx.lineWidth = 1;
      ctx.fillStyle = '#8e8e93';
      ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
      for (var i = 0; i <= 2; i++) {
        var val = minY + (maxY - minY) * i / 2;
        var yy = py(val);
        ctx.beginPath();
        ctx.moveTo(padding.left, yy);
        ctx.lineTo(cssWidth - padding.right, yy);
        ctx.stroke();
        ctx.fillText(fmtRangeVal(val), 2, yy + 4);
      }

      series.forEach(function(s) {
        if (s.points.length === 0) return;
        ctx.lineWidth = 2;
        for (var j = 0; j < s.points.length - 1; j++) {
          var p1 = s.points[j], p2 = s.points[j + 1];
          ctx.strokeStyle = STATUS_LINE_COLORS[p1.status] || '#007aff';
          ctx.beginPath();
          ctx.moveTo(px(p1.x), py(p1.y));
          ctx.lineTo(px(p2.x), py(p2.y));
          ctx.stroke();
        }
        s.points.forEach(function(p) {
          ctx.fillStyle = STATUS_LINE_COLORS[p.status] || '#007aff';
          ctx.beginPath();
          if (s.shape === 'square') {
            ctx.fillRect(px(p.x) - 3, py(p.y) - 3, 6, 6);
          } else {
            ctx.arc(px(p.x), py(p.y), 3, 0, 2 * Math.PI);
            ctx.fill();
          }
        });
      });

      ctx.fillStyle = '#8e8e93';
      if (spanDays <= 10) {
        // Короткий период — подписываем каждый день на оси X.
        var dayCount = Math.round(spanDays) + 1;
        var oneDay = 86400000;
        var startDay = new Date(minX); startDay.setHours(0, 0, 0, 0);
        var lastLabelX = -1000;
        for (var d = 0; d < dayCount; d++) {
          var ts = startDay.getTime() + d * oneDay;
          if (ts < minX - oneDay || ts > maxX + oneDay) { continue; }
          var xx = px(Math.max(minX, Math.min(maxX, ts)));
          if (xx - lastLabelX < 30) { continue; }
          lastLabelX = xx;
          ctx.textAlign = 'center';
          ctx.fillText(fmtDayTick(ts), Math.min(Math.max(xx, padding.left + 14), cssWidth - padding.right - 14), cssHeight - 4);
        }
      } else {
        ctx.textAlign = 'left';
        ctx.fillText(fmtDayTick(minX), padding.left, cssHeight - 4);
        ctx.textAlign = 'right';
        ctx.fillText(fmtDayTick(maxX), cssWidth - padding.right, cssHeight - 4);
      }
      ctx.textAlign = 'left';
    }

    function toTimestamp(v) {
      return new Date(v.replace(' ', 'T')).getTime();
    }

    function renderTrendCharts(entries) {
      var fastingPoints = entries
        .filter(function(e) { return e.type === 'glucose' && e.glucose_type === 'fasting'; })
        .map(function(e) { return { x: toTimestamp(e.measured_at), y: e.value_mmol_l, status: e.status }; })
        .sort(function(a, b) { return a.x - b.x; });
      var postPoints = entries
        .filter(function(e) { return e.type === 'glucose' && e.glucose_type === 'post_meal'; })
        .map(function(e) { return { x: toTimestamp(e.measured_at), y: e.value_mmol_l, status: e.status }; })
        .sort(function(a, b) { return a.x - b.x; });

      var gWrap = document.getElementById('trend-glucose-wrap');
      var hasGlucose = (fastingPoints.length + postPoints.length) >= 2;
      if (gWrap) { gWrap.hidden = !hasGlucose; }
      if (hasGlucose) {
        var gBands = [];
        if (RANGES.glucose_post) { gBands.push({ low: RANGES.glucose_post[0], high: RANGES.glucose_post[1], color: BAND_COLOR_OUTER }); }
        if (RANGES.glucose_fasting) { gBands.push({ low: RANGES.glucose_fasting[0], high: RANGES.glucose_fasting[1], color: BAND_COLOR_INNER }); }
        drawLineChart(document.getElementById('trend-glucose'), [
          { points: fastingPoints, shape: 'circle' },
          { points: postPoints, shape: 'square' }
        ], gBands);
      }

      var sysPoints = [], diaPoints = [];
      entries.filter(function(e) { return e.type === 'vitals'; }).forEach(function(e) {
        var t = toTimestamp(e.measured_at);
        sysPoints.push({ x: t, y: e.systolic_mmhg, status: e.systolic_status });
        diaPoints.push({ x: t, y: e.diastolic_mmhg, status: e.diastolic_status });
      });
      sysPoints.sort(function(a, b) { return a.x - b.x; });
      diaPoints.sort(function(a, b) { return a.x - b.x; });

      var vWrap = document.getElementById('trend-vitals-wrap');
      var hasVitals = sysPoints.length >= 2;
      if (vWrap) { vWrap.hidden = !hasVitals; }
      if (hasVitals) {
        var vBands = [];
        if (RANGES.systolic) { vBands.push({ low: RANGES.systolic[0], high: RANGES.systolic[1], color: BAND_COLOR_OUTER }); }
        if (RANGES.diastolic) { vBands.push({ low: RANGES.diastolic[0], high: RANGES.diastolic[1], color: BAND_COLOR_INNER }); }
        drawLineChart(document.getElementById('trend-vitals'), [
          { points: sysPoints, shape: 'circle' },
          { points: diaPoints, shape: 'square' }
        ], vBands);
      }
      var tempPoints = entries.filter(function(e) { return e.type === 'temperature'; }).map(function(e) { return { x: toTimestamp(e.measured_at), y: e.temperature_c, status: 'ok' }; }).sort(function(a,b){ return a.x-b.x; });
      var tWrap = document.getElementById('trend-temperature-wrap');
      var hasTemperature = tempPoints.length >= 2;
      if (tWrap) { tWrap.hidden = !hasTemperature; }
      if (hasTemperature) { drawLineChart(document.getElementById('trend-temperature'), [{ points: tempPoints, shape: 'circle' }], []); }

      var weightPoints = entries.filter(function(e) { return e.type === 'weight'; }).map(function(e) { return { x: toTimestamp(e.measured_at), y: e.weight_kg, status: 'ok' }; }).sort(function(a,b){ return a.x-b.x; });
      var wWrap = document.getElementById('trend-weight-wrap');
      var hasWeight = weightPoints.length >= 2;
      if (wWrap) { wWrap.hidden = !hasWeight; }
      if (hasWeight) { drawLineChart(document.getElementById('trend-weight'), [{ points: weightPoints, shape: 'circle' }], []); }
    }

    async function loadHistory() {
      var hp = historyParams();
      var df = hp.df;
      var dt = hp.dt;
      var cards = document.getElementById('history-cards');
      if (cards) { cards.innerHTML = '<div class="muted">Загрузка…</div>'; }
      try {
        var res = await fetch('/api/history?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort));
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        if (cards) { cards.innerHTML = ''; }
        var lastDay = null;
        var groupByDay = (hp.sort === 'date');

        function addDay(day) {
          if (!cards) return;
          var heading = document.createElement('div');
          heading.className = 'history-day-title';
          heading.innerHTML = '<span>' + fmtDateRuLong(day) + '</span>';
          var today = localDate(0), yesterday = localDate(-1);
          if (day === today) { heading.innerHTML += '<span class="day-chip">Сегодня</span>'; }
          else if (day === yesterday) { heading.innerHTML += '<span class="day-chip">Вчера</span>'; }
          cards.appendChild(heading);
        }

        function makeActionButton(text, cls, label, handler) {
          var b = document.createElement('button'); b.type='button'; b.className='icon-btn' + (cls ? ' '+cls : '');
          b.textContent=text; b.setAttribute('aria-label', label); b.addEventListener('click', handler); return b;
        }
        function statusBadge(status, text) {
          var span = document.createElement('span'); span.className='status-badge status-' + (status || 'ok'); span.textContent=text; return span;
        }
        function appendComment(parent, text) {
          if (!text) return; var c=document.createElement('div'); c.className='comment-line'; c.textContent=text; parent.appendChild(c);
        }
        function renderGlucose(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='💧'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Глюкоза'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.glucose_type === 'fasting' ? 'Натощак' : 'После еды') + ' · ' + (entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.value_mmol_l).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='ммоль/л'; val.appendChild(unit);
          card.appendChild(val);
          var text = entry.status === 'high' ? '▲ Выше диапазона' : entry.status === 'low' ? '▼ Ниже диапазона' : '✓ В пределах диапазона';
          card.appendChild(statusBadge(entry.status, text)); appendComment(card, entry.comment);
          return card;
        }
        function renderVitals(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='💓'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Давление и пульс'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var vals=document.createElement('div'); vals.className='vitals-values';
          var bp=document.createElement('div'); var lab=document.createElement('div'); lab.className='vital-label'; lab.textContent='Давление'; bp.appendChild(lab); var pn=document.createElement('div'); pn.className='pressure-number'; pn.textContent=entry.systolic_mmhg + ' / ' + entry.diastolic_mmhg; bp.appendChild(pn); var pu=document.createElement('div'); pu.className='pressure-unit'; pu.textContent='мм рт. ст.'; bp.appendChild(pu); vals.appendChild(bp);
          var div=document.createElement('div'); div.className='vitals-divider'; vals.appendChild(div);
          var pulse=document.createElement('div'); var pl=document.createElement('div'); pl.className='vital-label'; pl.textContent='Пульс'; pulse.appendChild(pl); var pnum=document.createElement('div'); pnum.className='pressure-number'; pnum.textContent=entry.pulse_bpm == null ? '—' : entry.pulse_bpm; pulse.appendChild(pnum); var punit=document.createElement('div'); punit.className='pulse-unit'; punit.textContent='уд/мин'; pulse.appendChild(punit); vals.appendChild(pulse); card.appendChild(vals);
          var statuses=document.createElement('div'); statuses.className='vital-statuses';
          function addVitalStatus(st, labelText, caption){ var item=document.createElement('div'); item.className='vital-status-item'; item.appendChild(statusBadge(st, st==='high'?'▲ Выше':st==='low'?'▼ Ниже':'✓ В норме')); var cap=document.createElement('div'); cap.className='vital-caption'; cap.textContent=caption; item.appendChild(cap); statuses.appendChild(item); }
          addVitalStatus(entry.systolic_status,'', 'систолическое'); addVitalStatus(entry.diastolic_status,'', 'диастолическое');
          if (entry.pulse_bpm != null) { addVitalStatus(entry.pulse_status || 'ok','', 'пульс'); } else { var empty=document.createElement('div'); empty.className='vital-status-item'; empty.innerHTML='<div class="vital-caption">Пульс не указан</div>'; statuses.appendChild(empty); }
          card.appendChild(statuses); appendComment(card, entry.comment);
          return card;
        }
        function renderTemperature(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon temperature'; icon.textContent='🌡️'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Температура'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.temperature_c).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='°C'; val.appendChild(unit); card.appendChild(val);
          appendComment(card, entry.comment); return card;
        }

        function renderWeight(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top';
          var icon=document.createElement('div'); icon.className='history-icon weight'; icon.textContent='⚖️'; top.appendChild(icon);
          var main=document.createElement('div'); main.className='history-entry-main';
          var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Вес'; main.appendChild(title);
          var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var val=document.createElement('div'); val.className='history-value';
          var num=document.createElement('span'); num.className='number'; num.textContent=Number(entry.weight_kg).toFixed(1); val.appendChild(num);
          var unit=document.createElement('span'); unit.className='unit'; unit.textContent='кг'; val.appendChild(unit); card.appendChild(val);
          appendComment(card, entry.comment); return card;
        }

        function renderFood(entry) {
          var card=document.createElement('article'); card.className='history-entry';
          var top=document.createElement('div'); top.className='history-entry-top'; var icon=document.createElement('div'); icon.className='history-icon'; icon.textContent='🥗'; top.appendChild(icon); var main=document.createElement('div'); main.className='history-entry-main'; var title=document.createElement('div'); title.className='history-entry-title'; title.textContent='Питание'; main.appendChild(title); var meta=document.createElement('div'); meta.className='history-entry-meta'; meta.textContent=(entry.measured_at ? entry.measured_at.substring(11,16) : ''); main.appendChild(meta); top.appendChild(main); card.appendChild(top);
          var line=document.createElement('div'); line.className='food-line'; line.textContent=entry.food_name + ' · '; var amount=document.createElement('span'); amount.className='food-amount'; amount.textContent=(Number(entry.amount_value).toLocaleString('ru-RU') + ' ' + unitRu(entry.amount_unit)); line.appendChild(amount); card.appendChild(line); appendComment(card, entry.comment); return card;
        }

        out.entries.forEach(function(entry) {
          if (groupByDay) { var day=(entry.measured_at||'').substring(0,10); if(day!==lastDay){lastDay=day; addDay(day);} }
          var card = entry.type==='glucose' ? renderGlucose(entry) : entry.type==='vitals' ? renderVitals(entry) : entry.type==='temperature' ? renderTemperature(entry) : entry.type==='weight' ? renderWeight(entry) : renderFood(entry);
          var actions=document.createElement('div'); actions.className='history-actions';
          actions.appendChild(makeActionButton('✏️','', 'Редактировать запись', function(){startEdit(entry);}));
          actions.appendChild(makeActionButton('🗑️','delete', 'Удалить запись', function(){deleteEntry(entry);})); card.appendChild(actions);
          if (cards) cards.appendChild(card);
        });

        if (out.entries.length === 0 && cards) { var empty=document.createElement('div'); empty.className='muted'; empty.style.padding='16px 2px'; empty.textContent='Нет записей за выбранный период'; cards.appendChild(empty); }
        var count=document.getElementById('history-count'); if(count) count.textContent='Записей: ' + out.entries.length;
        renderTrendCharts(out.entries);
        currentExportUrl = '/export.pdf?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(hp.type) + '&sort=' + encodeURIComponent(hp.sort);
        setMsg('history-msg', '', true);
      } catch (err) {
        if (cards) { cards.innerHTML=''; }
        var count=document.getElementById('history-count'); if(count) count.textContent='Записей: —';
        setMsg('history-msg', friendlyErrorMessage(err), false);
      }
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
      } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
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
      } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
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
        } catch (err) { setMsg('user-msg', friendlyErrorMessage(err), false); }
      });
      loadUsers();
    }

    async function loadBackupStatus() {
      if (!IS_ADMIN) return;
      try {
        var res = await fetch('/api/admin/backups');
        if (res.status === 401) { window.location = '/login'; return; }
        var out = await res.json();
        if (!res.ok) { throw new Error(out.error || ('HTTP ' + res.status)); }

        var statusEl = document.getElementById('backup-status');
        if (statusEl) {
          statusEl.textContent = out.enabled
            ? ('Автобэкап включён: каждый день в ' + out.scheduled_time + ' (время сервера), хранение ' + out.retention_days + ' дн., папка ' + out.backup_dir)
            : 'Автобэкап отключён на сервере (BACKUP_ENABLED=false)';
        }

        var tbody = document.querySelector('#backup-table tbody');
        tbody.innerHTML = '';
        if (out.backups.length === 0) {
          var tr0 = document.createElement('tr');
          var td0 = document.createElement('td');
          td0.colSpan = 3;
          td0.textContent = 'Копий пока нет';
          tr0.appendChild(td0);
          tbody.appendChild(tr0);
        }
        out.backups.forEach(function(b) {
          var tr = document.createElement('tr');
          var td1 = document.createElement('td');
          td1.textContent = b.name;
          tr.appendChild(td1);
          var td2 = document.createElement('td');
          td2.textContent = b.created_at;
          tr.appendChild(td2);
          var td3 = document.createElement('td');
          td3.textContent = (b.size_bytes / (1024 * 1024)).toFixed(1) + ' МБ';
          tr.appendChild(td3);

          var td4 = document.createElement('td');
          var restoreBtn = document.createElement('button');
          restoreBtn.type = 'button';
          restoreBtn.className = 'backup-restore-btn';
          restoreBtn.textContent = 'Восстановить';
          restoreBtn.addEventListener('click', function() { restoreBackup(b.name); });
          td4.appendChild(restoreBtn);
          tr.appendChild(td4);
          tbody.appendChild(tr);
        });
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
      }
    }

    async function runBackupNow() {
      try {
        var out = await sendJSON('POST', '/api/admin/backups/run', {});
        setMsg('backup-msg', 'Копия создана: ' + out.file, true);
        showToast('Резервная копия создана', true);
        loadBackupStatus();
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }

    async function restoreBackup(filename) {
      var warning = 'Восстановить базу данных из копии «' + filename + '»?\\n\\n'
        + 'Текущее состояние сначала будет сохранено в аварийную копию. '
        + 'Данные, созданные после выбранной резервной копии, будут заменены её содержимым.\\n\\n'
        + 'После восстановления потребуется повторно войти в приложение.';
      if (!window.confirm(warning)) return;

      try {
        var out = await sendJSON('POST', '/api/admin/backups/restore', { filename: filename });
        setMsg('backup-msg', 'База восстановлена из ' + out.restored_file
          + '. Аварийная копия: ' + out.emergency_backup, true);
        showToast('База восстановлена. Выполняется выход…', true);
        setTimeout(function() { window.location = '/login'; }, 900);
      } catch (err) {
        setMsg('backup-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }
    loadBackupStatus();

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
          setMsg('user-edit-msg', friendlyErrorMessage(err), false);
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

    var waBioSupported = false;

    function updateWaToggle(registered) {
      var toggle = document.getElementById('wa-toggle');
      if (!toggle) { return; }
      toggle.checked = !!registered;
      // Включить можно только там, где есть биометрия. Отключить —
      // это просто удаление ключей на сервере, для этого биометрия на
      // текущем устройстве не нужна, поэтому если ключ уже есть
      // (зарегистрирован хоть на каком-то устройстве), переключатель
      // остаётся доступен в любом случае.
      toggle.disabled = !registered && !waBioSupported;
    }

    function refreshWaStatus() {
      return fetch('/api/webauthn/status').then(function(r) { return r.json(); }).then(function(out) {
        updateWaToggle(!!out.registered);
        return !!out.registered;
      }).catch(function() { return null; });
    }

    (function() {
      var sum = document.getElementById('wa-summary');
      var label = document.getElementById('wa-toggle-label');
      var avail = document.getElementById('wa-availability');
      var n = biometricName();
      if (sum) { sum.textContent = '🔐 ' + n; }
      if (label) { label.textContent = 'Вход по ' + n; }

      var supportCheck = (window.PublicKeyCredential && PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable)
        ? PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable().catch(function() { return false; })
        : Promise.resolve(false);

      supportCheck.then(function(av) {
        waBioSupported = !!av;
        if (!av && avail) {
          avail.textContent = 'На этом устройстве нет биометрии — включить здесь нельзя, но отключить для аккаунта можно.';
        }
        // Реальное состояние ("включена ли биометрия") спрашиваем у
        // сервера — ключ мог быть зарегистрирован на другом устройстве
        // этого аккаунта, а отключение удаляет все ключи целиком.
        refreshWaStatus();
      });
    })();

    async function onWaToggleChange(el) {
      el.disabled = true;
      try {
        if (el.checked) {
          await waRegister();
        } else {
          if (!confirm('Отключить вход по биометрии для этого аккаунта?')) {
            el.checked = true;
            return;
          }
          await waDelete();
        }
      } finally {
        await refreshWaStatus();
      }
    }

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
        setMsg('wa-msg', friendlyErrorMessage(err), false);
      }
    }

    async function waDelete() {
      try {
        await sendJSON('DELETE', '/api/webauthn/credentials', {});
        try { localStorage.removeItem('medical_diary_wa_credential_id'); } catch (e) {}
        setMsg('wa-msg', 'Вход по биометрии отключён', true);
      } catch (err) {
        setMsg('wa-msg', friendlyErrorMessage(err), false);
      }
    }

    var UNIT_RU_JS = {
      'g':'г','gram':'г','grams':'г','kg':'кг','kilogram':'кг','kilograms':'кг',
      'mg':'мг','milligram':'мг','milligrams':'мг','mcg':'мкг','µg':'мкг',
      'ml':'мл','milliliter':'мл','milliliters':'мл','l':'л','liter':'л','liters':'л',
      'pcs':'шт','pc':'шт','piece':'шт','pieces':'шт',
      'portion':'порция','portions':'порция',
      'г':'г','кг':'кг','мг':'мг','мкг':'мкг','мл':'мл','л':'л','шт':'шт','порция':'порция'
    };
    function unitRu(value) { var k = String(value || '').trim(); return UNIT_RU_JS[k] || k; }
    var editState = { type: null, id: null };

    async function sendJSON(method, url, data, extraHeaders) {
      var res;
      try {
        res = await fetch(url, {
          method: method,
          headers: Object.assign({ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, extraHeaders || {}),
          body: JSON.stringify(data)
        });
      } catch (err) {
        throw new Error(friendlyErrorMessage(err));
      }

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
      document.getElementById('edit-temperature').hidden = entry.type !== 'temperature';
      document.getElementById('edit-weight').hidden = entry.type !== 'weight';
      document.getElementById('edit-food').hidden = entry.type !== 'food';
      document.getElementById('edit-title').textContent = '✏️ ' + entry.type_label + ' · ' + (entry.measured_at_ru || entry.measured_at);

      document.getElementById('edit_measured_at').value = entry.measured_at.replace(' ', 'T').substring(0, 16);
      document.getElementById('edit_comment').value = entry.comment || '';

      if (entry.type === 'glucose') {
        document.getElementById('edit_glucose_type').value = entry.glucose_type;
        document.getElementById('edit_glucose_value').value = entry.value_mmol_l;
      } else if (entry.type === 'vitals') {
        document.getElementById('edit_systolic').value = entry.systolic_mmhg;
        document.getElementById('edit_diastolic').value = entry.diastolic_mmhg;
        document.getElementById('edit_pulse').value = (entry.pulse_bpm == null) ? '' : entry.pulse_bpm;
      } else if (entry.type === 'temperature') {
        document.getElementById('edit_temperature_value').value = entry.temperature_c;
      } else if (entry.type === 'weight') {
        document.getElementById('edit_weight_value').value = entry.weight_kg;
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
      } else if (editState.type === 'temperature') {
        url = '/api/temperature/' + editState.id;
        payload = {
          value: document.getElementById('edit_temperature_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'weight') {
        url = '/api/weight/' + editState.id;
        payload = {
          value: document.getElementById('edit_weight_value').value,
          measured_at: ma,
          comment: cm
        };
      } else if (editState.type === 'food') {
        url = '/api/food/' + editState.id;
        payload = {
          food_name: document.getElementById('edit_food_name').value,
          amount_value: document.getElementById('edit_amount_value').value,
          amount_unit: unitRu(document.getElementById('edit_amount_unit').value),
          consumed_at: ma,
          comment: cm
        };
      }

      try {
        await sendJSON('PATCH', url, payload);
        setMsg('edit-msg', '', true);
        showToast('Изменения сохранены', true);
        closeEdit();
        loadHistory();
      } catch (err) {
        setMsg('edit-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
      }
    }

    async function deleteEntry(entry) {
      if (!confirm('Удалить запись «' + entry.type_label + '» от ' + (entry.measured_at_ru || entry.measured_at) + '? Она скроется из истории и PDF.')) return;

      var url = '';
      if (entry.type === 'glucose') { url = '/api/glucose/' + entry.id; }
      else if (entry.type === 'vitals') { url = '/api/vitals/' + entry.id; }
      else if (entry.type === 'temperature') { url = '/api/temperature/' + entry.id; }
      else if (entry.type === 'weight') { url = '/api/weight/' + entry.id; }
      else if (entry.type === 'food') { url = '/api/food/' + entry.id; }

      try {
        await sendJSON('DELETE', url, {});
        showToast('Запись удалена', true);
        loadHistory();
      } catch (err) {
        setMsg('history-msg', friendlyErrorMessage(err), false);
        showToast(friendlyErrorMessage(err), false);
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
    var pdfJsLoadPromise = null;

    function ensurePdfJsLoaded() {
      if (typeof pdfjsLib !== 'undefined') {
        try {
          if (pdfjsLib.GlobalWorkerOptions) { pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.js'; }
        } catch (e) {}
        return Promise.resolve(pdfjsLib);
      }
      if (pdfJsLoadPromise) { return pdfJsLoadPromise; }
      pdfJsLoadPromise = new Promise(function(resolve, reject) {
        var script = document.createElement('script');
        script.src = '/pdf.min.js';
        script.async = true;
        script.onload = function() {
          if (typeof pdfjsLib === 'undefined') {
            reject(new Error('Модуль PDF не загрузился')); return;
          }
          try {
            if (pdfjsLib.GlobalWorkerOptions) { pdfjsLib.GlobalWorkerOptions.workerSrc = '/pdf.worker.min.js'; }
          } catch (e) {}
          resolve(pdfjsLib);
        };
        script.onerror = function() { reject(new Error('Не удалось загрузить модуль PDF')); };
        document.head.appendChild(script);
      });
      return pdfJsLoadPromise;
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

    function openPdfTypeModal() {
      var currentType = (document.getElementById('history_type') || {}).value || 'all';
      var preselect = currentType === 'all' ? null : currentType.split(',');
      document.querySelectorAll('.pdf-type-cb').forEach(function (cb) {
        cb.checked = preselect ? (preselect.indexOf(cb.value) !== -1) : true;
      });
      setMsg('pdf-type-msg', '', true);
      document.getElementById('pdf-type-modal').hidden = false;
      document.body.style.overflow = 'hidden';
    }

    function closePdfTypeModal() {
      document.getElementById('pdf-type-modal').hidden = true;
      document.body.style.overflow = '';
    }

    document.getElementById('pdf-type-modal').addEventListener('click', function (e) {
      if (e.target === this) { closePdfTypeModal(); }
    });

    function confirmPdfTypeSelection() {
      var selected = Array.prototype.slice.call(document.querySelectorAll('.pdf-type-cb:checked')).map(function (cb) { return cb.value; });
      if (!selected.length) {
        setMsg('pdf-type-msg', 'Выберите хотя бы один тип записей', false);
        return;
      }

      var allValues = ['glucose', 'vitals', 'temperature', 'weight', 'food'];
      var typeParam = (selected.length === allValues.length) ? 'all' : selected.join(',');

      var df = document.getElementById('date_from').value;
      var dt = document.getElementById('date_to').value;
      var sortInput = document.querySelector('input[name="history_sort"]:checked');
      var sort = sortInput ? sortInput.value : 'date';

      currentExportUrl = '/export.pdf?date_from=' + encodeURIComponent(df) + '&date_to=' + encodeURIComponent(dt) + '&type=' + encodeURIComponent(typeParam) + '&sort=' + encodeURIComponent(sort);

      closePdfTypeModal();
      openPdfViewer();
    }

    async function openPdfViewer() {
      if (!currentExportUrl) { setMsg('history-msg', 'Сначала дождитесь загрузки истории', false); return; }

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
        var pdfLib = await ensurePdfJsLoaded();
        var res = await fetch(currentExportUrl);
        if (res.status === 401) { window.location = '/login'; return; }
        if (!res.ok) { throw new Error('HTTP ' + res.status); }
        pdfBlob = await res.blob();
        var data = await pdfBlob.arrayBuffer();
        pdfDoc = await pdfLib.getDocument({ data: data }).promise;
        pages.innerHTML = '';
        pdfPages = [];
        await renderPdfPages(1);
        bindPinch();
      } catch (err) {
        pages.innerHTML = '<div class="pdf-status">Ошибка просмотра: ' + friendlyErrorMessage(err) + '</div>';
      }
    }

    function downloadPdf() {
      if (!pdfBlob) {
        alert('PDF ещё не сформирован.');
        return;
      }
      try {
        var url = URL.createObjectURL(pdfBlob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'medical_diary.pdf';
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        setTimeout(function() {
          document.body.removeChild(a);
          URL.revokeObjectURL(url);
        }, 1000);
      } catch (err) {
        alert('Не удалось сохранить PDF: ' + friendlyErrorMessage(err));
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
        if (err && err.name !== 'AbortError') { alert('Не удалось поделиться: ' + friendlyErrorMessage(err)); }
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

    (function() {
  // details manual toggle: гарантированное сворачивание/разворачивание
  // блоков по тапу на заголовок, независимо от нативного поведения iOS.
  document.querySelectorAll('summary').forEach(function(s) {
    s.addEventListener('click', function(e) {
      var d = s.closest('details');
      if (!d) { return; }
      e.preventDefault();
      if (d.hasAttribute('open')) { d.removeAttribute('open'); } else { d.setAttribute('open', ''); }
    });
  });
})();

loadHistory();

(function(){
  var originalLoadLast=window.loadLastEntryStatuses;
  if(typeof originalLoadLast==='function') window.loadLastEntryStatuses=function(){originalLoadLast();loadDashboard();};
  document.addEventListener('visibilitychange',function(){if(!document.hidden)loadDashboard();});
})();
</script></body></html>
"""


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
