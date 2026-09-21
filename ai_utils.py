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
        "temperature_c": list(ranges.get("temperature", DEFAULT_RANGES["temperature"])),
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


# Знаков после запятой для округления статистики — как в существующем
# форматировании PDF/истории (pdf_export.py): глюкоза/температура/вес —
# 1 знак, давление и пульс — целые.
_DYNAMICS_ROUND_DIGITS = {
    "glucose_fasting": 1, "glucose_post": 1,
    "systolic": 0, "diastolic": 0, "pulse": 0,
    "temperature": 1, "weight": 1,
}


def compute_period_stats(entries):
    """Детерминированная (без ИИ) агрегация показателей за период.

    Вход — entries в формате query_entries()/add_assessments() (у каждой
    записи уже проставлено entry["assessment_status"] по тем же
    справочным диапазонам, что использует остальное приложение). Функция
    НЕ полагается на порядок entries — для "первого"/"последнего"
    значения внутри группы сортирует по measured_at сама.

    Возвращает ТОЛЬКО факты и расчёты (count/min/avg/max/первое-
    последнее/дельта/распределение по статусу диапазона) — без единого
    слова интерпретации. Разделение факт/расчёт/вывод сделано намеренно:
    любую интерпретацию в безопасной формулировке добавляет отдельно
    GigaChat (assessments.add_ai_dynamics_summary), и модели передаются
    ТОЛЬКО эти уже посчитанные числа — ни одной сырой записи, ни
    комментариев пользователя, ни ID.

    Для веса и питания статус диапазона не считается: как и в
    weight_assessment()/food_assessment(), для них нет универсальной
    "нормы", поэтому status_counts для них не добавляется.
    """
    groups = {}

    def push(key, entry, value_field, with_status):
        try:
            value = float(entry.get(value_field))
        except (TypeError, ValueError):
            return
        measured_at = entry.get("measured_at")
        if not measured_at:
            return
        status = entry.get("assessment_status") if with_status else None
        groups.setdefault(key, []).append((measured_at, value, status))

    for e in entries:
        entry_type = e.get("type")
        if entry_type == "glucose":
            sub = "glucose_fasting" if e.get("glucose_type") == "fasting" else "glucose_post"
            push(sub, e, "value_mmol_l", with_status=True)
        elif entry_type == "vitals":
            push("systolic", e, "systolic_mmhg", with_status=True)
            push("diastolic", e, "diastolic_mmhg", with_status=True)
            if e.get("pulse_bpm") is not None:
                push("pulse", e, "pulse_bpm", with_status=True)
        elif entry_type == "temperature":
            push("temperature", e, "temperature_c", with_status=True)
        elif entry_type == "weight":
            push("weight", e, "weight_kg", with_status=False)

    stats = {}
    for key, points in groups.items():
        points_sorted = sorted(points, key=lambda p: p[0])
        values = [v for _, v, _ in points_sorted]
        digits = _DYNAMICS_ROUND_DIGITS.get(key, 1)

        block = {
            "count": len(values),
            "min": round(min(values), digits),
            "max": round(max(values), digits),
            "avg": round(sum(values) / len(values), digits),
            "first_value": round(points_sorted[0][1], digits),
            "first_at": points_sorted[0][0],
            "last_value": round(points_sorted[-1][1], digits),
            "last_at": points_sorted[-1][0],
        }
        if len(values) >= 2:
            block["delta"] = round(block["last_value"] - block["first_value"], digits)

        if points_sorted[0][2] is not None:
            counts = {"low": 0, "ok": 0, "high": 0}
            for _, _, status in points_sorted:
                if status in counts:
                    counts[status] += 1
            block["status_counts"] = counts

        stats[key] = block

    food_count = sum(1 for e in entries if e.get("type") == "food")
    if food_count:
        stats["food"] = {"count": food_count}

    return stats


def _dynamics_input_hash(stats, ranges_payload, date_from, date_to, entry_type):
    """Хэш входных данных для кэша ai_dynamics_cache.

    Пока агрегированная статистика периода, диапазоны, границы периода и
    фильтр типа не изменились — повторный запрос к GigaChat не нужен,
    возвращается сохранённый текст (см. assessments.add_ai_dynamics_summary).
    """
    blob = json.dumps(
        {
            "stats": stats,
            "ranges": ranges_payload,
            "date_from": date_from,
            "date_to": date_to,
            "type": entry_type,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
