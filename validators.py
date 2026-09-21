"""Чистые функции разбора и форматирования — без обращений к Flask
(session/g/request), к базе данных или к сети. На вход — только
аргументы, на выход — только возвращаемое значение или ValueError.

Извлечено из app.py на шаге 3 модуляризации.
"""

from datetime import datetime, date


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
    "temperature": (35.0, 37.0),
}


# Температура от этого значения всегда считается высокой — независимо от
# персонального диапазона (аналог safety-override для давления).
TEMPERATURE_HIGH_ALERT_C = 38.0


MONTHS_RU_SHORT = ["янв.", "февр.", "мар.", "апр.", "мая", "июня", "июля", "авг.", "сент.", "окт.", "нояб.", "дек."]


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


def parse_iso_date(value, default_date):
    if value is None or str(value).strip() == "":
        return default_date

    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise ValueError("Некорректная дата")


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


def format_dt_ru(value):
    try:
        s = str(value)
        d = date.fromisoformat(s[:10])
        t = s[11:16]
        return f"{d.day:02d} {MONTHS_RU_SHORT[d.month - 1]} {d.year % 100:02d} г. {t}"
    except Exception:
        return str(value)


def status_of(value, low, high):
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "ok"
