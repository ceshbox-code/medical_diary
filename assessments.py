"""Оценка показателей: встроенные правила + ИИ-оценка через GigaChat.

Извлечено из app.py на шаге 5 модуляризации.

- glucose_assessment / vitals_assessment / temperature_assessment /
  food_assessment / weight_assessment / add_assessments — детерминированная
  оценка на жёстких правилах, без сети, без побочных эффектов.
- add_ai_assessments / _gigachat_generate_assessments / _gigachat_get_access_token /
  _gigachat_invalidate_token — интеграция с GigaChat: OAuth-токен (с кэшем
  и самовосстановлением при 401), кэш ИИ-оценок в БД (ai_assessment_cache),
  батчинг запросов. ИИ не ставит диагнозы и не назначает лечение — это
  инвариант, закреплённый в system-промпте (_gigachat_generate_assessments)
  и в safety-override для критического давления.
"""

import os
import time
import json
import threading
import urllib.request
import urllib.error
import urllib.parse
import uuid

from flask import session

from db import get_db
from validators import DEFAULT_RANGES, TEMPERATURE_HIGH_ALERT_C, status_of
from ai_utils import (
    _ai_ranges_payload,
    _ai_input_hash,
    _ai_blocked_by_safety_override,
    _dynamics_input_hash,
)


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


def temperature_assessment(value_c, ranges=None):
    """Справочная оценка температуры без постановки диагноза.

    Границы «ориентировочного диапазона» берутся из персональных настроек
    (ranges["temperature"]); по умолчанию 35.0–37.0 °C, как и раньше.
    Порог 38.0 °C от настроек не зависит.
    """
    ranges = ranges or DEFAULT_RANGES
    try:
        low, high = ranges.get("temperature", DEFAULT_RANGES["temperature"])
        low, high = float(low), float(high)
    except (TypeError, ValueError):
        low, high = DEFAULT_RANGES["temperature"]

    try:
        value = float(value_c)
    except (TypeError, ValueError):
        return "Значение температуры не распознано", "Проверьте измерение и единицы (°C).", "ok"

    if value >= TEMPERATURE_HIGH_ALERT_C:
        return (
            "Температура высокая",
            "Повторите измерение и оцените самочувствие; при сохранении высокой температуры или ухудшении состояния обратитесь за медицинской помощью.",
            "high",
        )
    if value < low:
        return (
            "Температура ниже ориентировочного диапазона",
            "Повторите измерение и оцените самочувствие; при выраженной слабости или ухудшении состояния обратитесь за медицинской помощью.",
            "low",
        )
    if value <= high:
        return "Температура в пределах ориентировочного диапазона", "Продолжайте наблюдение с учётом самочувствия.", "ok"
    return (
        "Температура повышена относительно ориентировочного диапазона",
        "Повторите измерение через некоторое время и наблюдайте за самочувствием; при сохранении повышения обратитесь к врачу.",
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


AI_DYNAMICS_COOLDOWN_SECONDS = max(1, int(os.getenv("AI_DYNAMICS_COOLDOWN_SECONDS", "20")))
AI_DYNAMICS_HOURLY_LIMIT = max(1, int(os.getenv("AI_DYNAMICS_HOURLY_LIMIT", "20")))


def _gigachat_generate_dynamics_summary(stats, ranges_payload, date_from, date_to):
    """Просит GigaChat сформулировать безопасный текст по УЖЕ ПОСЧИТАННОЙ
    в Python статистике периода (см. ai_utils.compute_period_stats).

    Модели передаются только агрегированные числа и справочные
    диапазоны — ни одной сырой записи дневника, ни комментариев
    пользователя, ни идентификаторов. System-промпт прямо запрещает
    придумывать данные и пересчитывать переданные числа: задача модели —
    только нейтральная формулировка того, что уже вычислено. Как и
    остальные ИИ-вызовы в проекте, ошибка любого рода не поднимается
    наверх — вызывающая сторона получает None и показывает пользователю
    понятное сообщение без падения запроса.
    """
    if not (GIGACHAT_AI_ENABLED and GIGACHAT_AUTH_KEY and stats):
        return None

    schema = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "maxLength": 900},
            "observations": {
                "type": "array",
                "items": {"type": "string", "maxLength": 200},
                "maxItems": 6,
            },
            "caution": {"type": "string", "maxLength": 300},
        },
        "required": ["summary", "observations", "caution"],
        "additionalProperties": False,
    }

    system_prompt = (
        "Ты описываешь уже посчитанную статистику показателей медицинского дневника за период. "
        "Тебе присланы только готовые агрегаты (count/min/avg/max/first_value/last_value/delta/"
        "status_counts) — не ставь диагнозы, не назначай, не отменяй и не изменяй лечение или "
        "лекарства. Используй только переданные числа: ничего не пересчитывай и не придумывай "
        "значения, которых нет во входных данных. Если каких-то данных недостаточно для "
        "содержательного наблюдения — прямо скажи об этом, не домысливая. "
        "summary — краткое нейтральное описание динамики за период целиком, не более 900 символов. "
        "observations — до 6 отдельных коротких наблюдений (не более 200 символов каждое), как "
        "правило по одному на показатель, без домыслов и без повтора всех чисел подряд. "
        "caution — не более 300 символов: если в status_counts заметная доля значений 'high' или "
        "'low', мягко порекомендуй обсудить это с врачом; если поводов нет — верни пустую строку. "
        "Не смягчай рекомендацию при систолическом давлении >=180 или диастолическом >=120, если "
        "такие значения видны в диапазонах или статистике. "
        "Ответь только JSON по заданной схеме, без диагнозов и назначений."
    )

    payload = {
        "period": {"date_from": date_from, "date_to": date_to},
        "stats": stats,
        "ranges": ranges_payload,
    }
    prompt = system_prompt + "\n\nДанные:\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    try:
        token = _gigachat_get_access_token()
        if not token:
            return None

        body = {
            "model": GIGACHAT_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1200,
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

        summary = _normalize_ai_text(parsed.get("summary"), 900)
        if not summary:
            return None

        observations = []
        observations_raw = parsed.get("observations")
        if isinstance(observations_raw, list):
            for item in observations_raw[:6]:
                normalized = _normalize_ai_text(item, 200)
                if normalized:
                    observations.append(normalized)

        caution = _normalize_ai_text(parsed.get("caution"), 300)

        return {"summary": summary, "observations": observations, "caution": caution}

    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 401:
            _gigachat_invalidate_token()
        print(f"[gigachat] dynamics summary failed: {type(exc).__name__}: {exc}", flush=True)
        return None


def add_ai_dynamics_summary(stats, ranges_payload, date_from, date_to, entry_type, enabled=True):
    """Публичная точка входа для вкладки «История» → «Оценка динамики от ИИ».

    По аналогии с add_ai_assessments: кэш в БД (ai_dynamics_cache) плюс
    троттлинг «свежих» (не из кэша) обращений к GigaChat
    (ai_dynamics_throttle), но не по одной записи, а по агрегатам целого
    периода (см. ai_utils.compute_period_stats/_dynamics_input_hash).

    Возвращает словарь:
        {"ok": bool, "cached": bool, "summary": str, "observations": [...],
         "caution": str, "error": str | None}
    error, если ok=False: "disabled" (ИИ выключен/не настроен),
    "throttled" (слишком частые запросы) или "gigachat_failed" (сбой
    самого запроса к GigaChat). Отсутствие ответа ИИ никогда не должно
    приводить к падению запроса — вызывающий код (app.py) просто
    показывает пользователю понятное сообщение по коду ошибки.
    """
    if not (enabled and GIGACHAT_AI_ENABLED and GIGACHAT_AUTH_KEY and stats):
        return {"ok": False, "cached": False, "error": "disabled"}

    user_id = session.get("user_id")
    input_hash = _dynamics_input_hash(stats, ranges_payload, date_from, date_to, entry_type)
    db = get_db()

    if user_id:
        try:
            cached = db.execute(
                "SELECT summary, observations_json, caution FROM ai_dynamics_cache "
                "WHERE user_id = ? AND input_hash = ?",
                (user_id, input_hash),
            ).fetchone()
        except Exception:
            cached = None

        if cached:
            try:
                observations = json.loads(cached["observations_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                observations = []
            return {
                "ok": True,
                "cached": True,
                "summary": cached["summary"],
                "observations": observations,
                "caution": cached["caution"] or "",
                "error": None,
            }

    if user_id:
        try:
            recent = db.execute(
                "SELECT COUNT(*) AS c FROM ai_dynamics_throttle "
                "WHERE user_id = ? AND requested_at > datetime('now', ?)",
                (user_id, f"-{AI_DYNAMICS_COOLDOWN_SECONDS} seconds"),
            ).fetchone()
            if recent and recent["c"] > 0:
                return {"ok": False, "cached": False, "error": "throttled"}

            hourly = db.execute(
                "SELECT COUNT(*) AS c FROM ai_dynamics_throttle "
                "WHERE user_id = ? AND requested_at > datetime('now', '-1 hour')",
                (user_id,),
            ).fetchone()
            if hourly and hourly["c"] >= AI_DYNAMICS_HOURLY_LIMIT:
                return {"ok": False, "cached": False, "error": "throttled"}

            # Строка троттлинга пишется ДО обращения к GigaChat и остаётся
            # даже при сбое запроса — иначе повторяющиеся ошибки можно было
            # бы использовать, чтобы обходить лимит частыми повторами.
            db.execute("INSERT INTO ai_dynamics_throttle (user_id) VALUES (?)", (user_id,))
            db.commit()
        except Exception:
            pass

    result = _gigachat_generate_dynamics_summary(stats, ranges_payload, date_from, date_to)
    if result is None:
        return {"ok": False, "cached": False, "error": "gigachat_failed"}

    if user_id:
        try:
            db.execute(
                """
                INSERT INTO ai_dynamics_cache
                    (user_id, input_hash, summary, observations_json, caution, model)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, input_hash) DO UPDATE SET
                    summary = excluded.summary,
                    observations_json = excluded.observations_json,
                    caution = excluded.caution,
                    model = excluded.model,
                    updated_at = datetime('now')
                """,
                (
                    user_id, input_hash, result["summary"],
                    json.dumps(result["observations"], ensure_ascii=False),
                    result.get("caution", ""), GIGACHAT_MODEL,
                ),
            )
            db.commit()
        except Exception:
            pass

    return {
        "ok": True,
        "cached": False,
        "summary": result["summary"],
        "observations": result["observations"],
        "caution": result.get("caution", ""),
        "error": None,
    }


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
                assessment, recommendation, status = temperature_assessment(e.get("temperature_c"), ranges)
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
