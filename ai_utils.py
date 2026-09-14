"""Чистые вспомогательные функции для ИИ-оценок (GigaChat) — без сетевых
вызовов и без обращений к Flask/БД. Собственно запрос к GigaChat
(_gigachat_generate_assessments, add_ai_assessments) остаётся в app.py —
он будет вынесен отдельно на шаге про AI-модуль.

Извлечено из app.py на шаге 3 модуляризации.
"""

import hashlib
import json

from validators import DEFAULT_RANGES


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
    передаются модели (см. _ai_entry_context в app.py).
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
