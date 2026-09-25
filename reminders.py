"""Напоминания и график приёма лекарств — шаг 1: хранение и API.

Что здесь есть:
  * лекарства (название, доза, инструкция — всё вводит пользователь) и их
    график (времена суток + дни недели);
  * ЖУРНАЛ ФАКТОВ приёма (принял / пропустил) со снимком названия и дозы
    на момент отметки — изменение дозы лекарства позже не переписывает историю;
  * напоминания об измерениях (глюкоза, давление, вес, температура, питание,
    своё).

Что здесь сознательно НЕТ:
  * никаких рекомендаций по дозам, проверки взаимодействий и оценок
    назначений — сервис только хранит то, что ввёл пользователь;
  * автоматической записи «не принял»: если время прошло, а отметки нет,
    интерфейс получает расчётное состояние "unmarked" (state_basis =
    "calculated"), а НЕ факт. Факт — только то, что пользователь отметил
    сам (state_basis = "recorded");
  * отправки push — это следующий шаг (планировщик и Web Push).

Время хранится так же, как в остальных таблицах приложения: локальное
«настенное» время строкой "YYYY-MM-DD HH:MM:SS" без часового пояса.
Дни недели: 0 = понедельник … 6 = воскресенье (как datetime.weekday()).

Blueprint регистрируется в app.py: `app.register_blueprint(reminders_bp)`.
CSRF-защита и заголовки безопасности действуют на уровне приложения.
"""

import os
import sqlite3
from datetime import datetime, timedelta, date

from flask import Blueprint, jsonify, request, session

from db import get_db
from security import (
    audit,
    login_required,
    get_idempotent_response,
    store_idempotent_response,
)
from validators import parse_dt, parse_iso_date, parse_float

reminders_bp = Blueprint("reminders", __name__)

# Единицы дозы — закрытый список, чтобы не копить в базе произвольный текст.
MED_UNITS = ("мг", "мкг", "г", "мл", "шт", "капли", "ЕД")

# Источник записи о лекарстве — закрытый список; всё, что не входит в
# перечень, тихо становится 'manual' (см. api_medication_create).
ALLOWED_MED_SOURCES = ("manual", "barcode_scan")

REMINDER_KINDS = {
    "glucose": "Измерить глюкозу",
    "vitals": "Измерить давление и пульс",
    "weight": "Взвеситься",
    "temperature": "Измерить температуру",
    "food": "Записать приём пищи",
    "custom": None,  # для «своего» напоминания название обязательно
}

MAX_MEDICATIONS = 100
MAX_TIMES_PER_MEDICATION = 12
MAX_REMINDERS = 100
ALL_DAYS_MASK = 127


def _read_int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# Через сколько минут после планового времени неотмеченный приём
# показывается как "unmarked". Это только отображение, в БД ничего не пишется.
UNMARKED_AFTER_MIN = _read_int_env("MED_UNMARKED_AFTER_MIN", 60)


def _now():
    return datetime.now()


# ---------------------------------------------------------------- разбор входа

def _json_body():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        return None
    return data


def _bad_request():
    return jsonify(error="Некорректный запрос"), 400


def _parse_taken_at(raw):
    """Фактическое время приёма. Пусто -> текущее время сервера (единый источник
    времени — _now()). Время в будущем (с запасом 5 мин на рассинхрон часов) отклоняется."""
    if raw is None or str(raw).strip() == "":
        taken = _now().strftime("%Y-%m-%d %H:%M:%S")
    else:
        taken = parse_dt(raw)
    if datetime.strptime(taken, "%Y-%m-%d %H:%M:%S") > _now() + timedelta(minutes=5):
        raise ValueError("Время приёма не может быть в будущем")
    return taken


def _text(value, max_len, field, required=False):
    s = "" if value is None else str(value).strip()
    if required and not s:
        raise ValueError(f"{field}: заполните поле")
    if len(s) > max_len:
        # Не обрезаем молча: медицинский текст не должен меняться без ведома.
        raise ValueError(f"{field}: не длиннее {max_len} символов")
    return s


def _parse_time(value, field="Время"):
    s = str(value or "").strip()
    try:
        return datetime.strptime(s, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"{field}: укажите время в формате ЧЧ:ММ")


def _parse_times(value):
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Время приёма: ожидается список")
    times = sorted({_parse_time(v, "Время приёма") for v in value})
    if len(times) > MAX_TIMES_PER_MEDICATION:
        raise ValueError(f"Время приёма: не более {MAX_TIMES_PER_MEDICATION} значений")
    return times


def _parse_days(value):
    """Список дней (0..6) -> битовая маска. None -> все дни."""
    if value is None:
        return ALL_DAYS_MASK
    if not isinstance(value, list) or not value:
        raise ValueError("Дни недели: выберите хотя бы один день")
    mask = 0
    for d in value:
        if isinstance(d, bool) or not isinstance(d, int) or d < 0 or d > 6:
            raise ValueError("Дни недели: допустимы числа 0–6 (0 = понедельник)")
        mask |= 1 << d
    return mask


def _mask_to_days(mask):
    return [d for d in range(7) if mask & (1 << d)]


def _parse_bool(value, field):
    if isinstance(value, bool):
        return 1 if value else 0
    if value in (0, 1):
        return int(value)
    raise ValueError(f"{field}: ожидается true или false")


# ------------------------------------------------------------------- лекарства

def _merge_medication(data, cur):
    """Собирает итоговые значения полей лекарства: то, что пришло в data,
    поверх текущих значений cur (при создании cur = None)."""
    base = cur or {
        "name": None, "dose_value": None, "dose_unit": None,
        "instructions": "", "start_date": None, "end_date": None,
        "is_active": 1, "comment": "", "days_mask": ALL_DAYS_MASK,
    }
    out = dict(base)

    if "name" in data:
        out["name"] = _text(data["name"], 100, "Название", required=True)
    if not out["name"]:
        raise ValueError("Название: заполните поле")

    if "dose_value" in data or "dose_unit" in data:
        raw_v = data.get("dose_value", base["dose_value"])
        raw_u = data.get("dose_unit", base["dose_unit"])
        if raw_v is None or str(raw_v).strip() == "":
            if raw_u is not None and str(raw_u).strip() != "":
                raise ValueError("Доза: укажите значение дозы")
            out["dose_value"], out["dose_unit"] = None, None
        else:
            out["dose_value"] = parse_float(raw_v, 0.001, 100000, "Доза")
            unit = str(raw_u or "").strip()
            if unit not in MED_UNITS:
                raise ValueError("Доза: выберите единицу (" + ", ".join(MED_UNITS) + ")")
            out["dose_unit"] = unit

    if "instructions" in data:
        out["instructions"] = _text(data["instructions"], 200, "Инструкция")
    if "comment" in data:
        out["comment"] = _text(data["comment"], 500, "Комментарий")

    default_start = date.fromisoformat(base["start_date"]) if base["start_date"] else _now().date()
    if "start_date" in data:
        start = parse_iso_date(data["start_date"], default_start)
    else:
        start = default_start
    out["start_date"] = start.isoformat()

    if "end_date" in data:
        raw_end = data["end_date"]
        out["end_date"] = None if raw_end is None or str(raw_end).strip() == "" else parse_iso_date(raw_end, None).isoformat()
    if out["end_date"] and out["end_date"] < out["start_date"]:
        raise ValueError("Дата окончания раньше даты начала")

    if "is_active" in data:
        out["is_active"] = _parse_bool(data["is_active"], "Активность")
    if "days" in data:
        out["days_mask"] = _parse_days(data["days"])
    return out


def _load_times(db, med_ids):
    if not med_ids:
        return {}
    placeholders = ",".join("?" * len(med_ids))
    rows = db.execute(
        f"SELECT medication_id, time_of_day FROM medication_schedule "
        f"WHERE medication_id IN ({placeholders}) ORDER BY time_of_day",
        list(med_ids),
    ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["medication_id"], []).append(r["time_of_day"])
    return result


def _med_to_json(row, times):
    return {
        "id": row["id"],
        "name": row["name"],
        "dose_value": row["dose_value"],
        "dose_unit": row["dose_unit"],
        "instructions": row["instructions"] or "",
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "is_active": bool(row["is_active"]),
        "comment": row["comment"] or "",
        "days": _mask_to_days(row["days_mask"]),
        "times": times,
    }


def _get_med(db, med_id):
    return db.execute(
        "SELECT * FROM medications WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (med_id, session["user_id"]),
    ).fetchone()


@reminders_bp.get("/api/medications")
@login_required
def api_medications_list():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM medications WHERE user_id = ? AND deleted_at IS NULL ORDER BY is_active DESC, name COLLATE NOCASE, id",
        (session["user_id"],),
    ).fetchall()
    times = _load_times(db, [r["id"] for r in rows])
    return jsonify(medications=[_med_to_json(r, times.get(r["id"], [])) for r in rows])


@reminders_bp.post("/api/medications")
@login_required
def api_medication_create():
    data = _json_body()
    if data is None:
        return _bad_request()
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    cached = get_idempotent_response(session["user_id"], "api_medication_create", idem_key)
    if cached is not None:
        return jsonify(cached)

    try:
        fields = _merge_medication(data, None)
        times = _parse_times(data.get("times"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    # Источник не влияет на валидацию и не выбирается пользователем в форме —
    # это факт о происхождении записи (обычный ввод или подстановка после
    # скана штрихкода). Всё, кроме известных значений, тихо становится 'manual'.
    source = data.get("source") if data.get("source") in ALLOWED_MED_SOURCES else "manual"

    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM medications WHERE user_id = ? AND deleted_at IS NULL",
        (session["user_id"],),
    ).fetchone()["c"]
    if count >= MAX_MEDICATIONS:
        return jsonify(error=f"Достигнут предел: {MAX_MEDICATIONS} лекарств"), 400

    cur = db.execute(
        "INSERT INTO medications (user_id, name, dose_value, dose_unit, instructions, start_date, end_date, is_active, comment, days_mask, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session["user_id"], fields["name"], fields["dose_value"], fields["dose_unit"],
         fields["instructions"], fields["start_date"], fields["end_date"], fields["is_active"],
         fields["comment"], fields["days_mask"], source),
    )
    med_id = cur.lastrowid
    db.executemany(
        "INSERT INTO medication_schedule (medication_id, time_of_day) VALUES (?, ?)",
        [(med_id, t) for t in times],
    )
    db.commit()

    audit("create_medication", "medications", med_id, {"times": len(times), "source": source})
    result = {"ok": True, "id": med_id}
    store_idempotent_response(session["user_id"], "api_medication_create", idem_key, "medications", med_id, result)
    return jsonify(result)


@reminders_bp.patch("/api/medications/<int:med_id>")
@login_required
def api_medication_update(med_id):
    data = _json_body()
    if data is None:
        return _bad_request()
    db = get_db()
    row = _get_med(db, med_id)
    if not row:
        return jsonify(error="Лекарство не найдено"), 404

    try:
        fields = _merge_medication(data, dict(row))
        times = _parse_times(data["times"]) if "times" in data else None
    except ValueError as e:
        return jsonify(error=str(e)), 400

    changed = [k for k in fields if fields[k] != row[k]]
    if times is not None:
        old_times = _load_times(db, [med_id]).get(med_id, [])
        if times != old_times:
            changed.append("times")

    db.execute(
        "UPDATE medications SET name = ?, dose_value = ?, dose_unit = ?, instructions = ?, start_date = ?, "
        "end_date = ?, is_active = ?, comment = ?, days_mask = ?, updated_at = datetime('now') WHERE id = ?",
        (fields["name"], fields["dose_value"], fields["dose_unit"], fields["instructions"],
         fields["start_date"], fields["end_date"], fields["is_active"], fields["comment"],
         fields["days_mask"], med_id),
    )
    if times is not None:
        db.execute("DELETE FROM medication_schedule WHERE medication_id = ?", (med_id,))
        db.executemany(
            "INSERT INTO medication_schedule (medication_id, time_of_day) VALUES (?, ?)",
            [(med_id, t) for t in times],
        )
    db.commit()
    audit("update_medication", "medications", med_id, {"fields": changed})
    return jsonify(ok=True)


@reminders_bp.delete("/api/medications/<int:med_id>")
@login_required
def api_medication_delete(med_id):
    db = get_db()
    row = _get_med(db, med_id)
    if not row:
        return jsonify(error="Лекарство не найдено"), 404
    # Мягкое удаление: журнал приёмов сохраняется (там есть снимок названия и дозы).
    db.execute(
        "UPDATE medications SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (med_id,),
    )
    db.commit()
    audit("delete_medication", "medications", med_id, {})
    return jsonify(ok=True)


# --------------------------------------------- справочник «GTIN -> название»
#
# Помогает быстрее заносить название лекарства: пользователь сканирует
# штрихкод/DataMatrix упаковки на клиенте (сам код декодируется в браузере,
# сюда попадает уже готовый GTIN), один раз подтверждает название и дозу —
# дальше при повторном скане той же упаковки они подставляются сами.
# Справочник строго персональный (привязан к user_id), к внешним реестрам
# лекарств (ИС МДЛП/«Честный знак» и т.п.) сервис не обращается.

def _valid_gtin(raw):
    s = str(raw or "").strip()
    if not s.isdigit() or not (8 <= len(s) <= 14):
        raise ValueError("Код товара: ожидается GTIN (8–14 цифр)")
    return s


@reminders_bp.get("/api/medication-barcodes/<gtin>")
@login_required
def api_medication_barcode_lookup(gtin):
    try:
        gtin = _valid_gtin(gtin)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    db = get_db()

    # 1. Личный справочник пользователя — то, что он сам подтвердил
    # раньше, приоритетнее официальных данных (мог поправить написание).
    row = db.execute(
        "SELECT name, dose_value, dose_unit FROM medication_barcodes WHERE user_id = ? AND gtin = ?",
        (session["user_id"], gtin),
    ).fetchone()
    if row:
        return jsonify(found=True, source="personal",
                        name=row["name"], dose_value=row["dose_value"], dose_unit=row["dose_unit"])

    # 2. Официальные открытые данные ЦРПТ (см. import_mdlp_gtins.py).
    # Дозу отсюда отдаём только как текстовую подсказку (dose_hint) —
    # формат свободный ("0.25 мг/г", "250 МЕ" и т.п.), надёжно разложить
    # его в наши value/unit нельзя, а молча подставлять непроверенную
    # дозу в медицинском контексте небезопасно.
    ref = db.execute(
        "SELECT prod_sell_name, dose_raw, form_name FROM mdlp_gtins WHERE gtin = ?",
        (gtin,),
    ).fetchone()
    if ref and ref["prod_sell_name"]:
        return jsonify(found=True, source="mdlp",
                        name=ref["prod_sell_name"], dose_hint=ref["dose_raw"], form_hint=ref["form_name"])

    return jsonify(found=False)


@reminders_bp.put("/api/medication-barcodes/<gtin>")
@login_required
def api_medication_barcode_save(gtin):
    data = _json_body()
    if data is None:
        return _bad_request()
    try:
        gtin = _valid_gtin(gtin)
        name = _text(data.get("name"), 100, "Название", required=True)
        dose_value = dose_unit = None
        raw_v = data.get("dose_value")
        if raw_v is not None and str(raw_v).strip() != "":
            dose_value = parse_float(raw_v, 0.001, 100000, "Доза")
            dose_unit = str(data.get("dose_unit") or "").strip()
            if dose_unit not in MED_UNITS:
                raise ValueError("Доза: выберите единицу (" + ", ".join(MED_UNITS) + ")")
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db = get_db()
    db.execute(
        "INSERT INTO medication_barcodes (user_id, gtin, name, dose_value, dose_unit) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, gtin) DO UPDATE SET "
        "name = excluded.name, dose_value = excluded.dose_value, dose_unit = excluded.dose_unit, "
        "updated_at = datetime('now')",
        (session["user_id"], gtin, name, dose_value, dose_unit),
    )
    row_id = db.execute(
        "SELECT id FROM medication_barcodes WHERE user_id = ? AND gtin = ?",
        (session["user_id"], gtin),
    ).fetchone()["id"]
    db.commit()
    audit("save_medication_barcode", "medication_barcodes", row_id, {"gtin": gtin})
    return jsonify(ok=True)


@reminders_bp.get("/api/medications/suggest")
@login_required
def api_medications_suggest():
    """Подсказки по названию при ручном вводе — по локальному кэшу открытых
    данных ЦРПТ (mdlp_gtins), без обращения куда-либо в сеть. Не персональные
    данные: это справочник лекарств, а не записи пользователя."""
    q = (request.args.get("q") or "").strip().lower()
    if len(q) < 2:
        return jsonify(items=[])
    like = "%" + q.replace("%", "").replace("_", "") + "%"
    rows = get_db().execute(
        "SELECT DISTINCT prod_sell_name FROM mdlp_gtins "
        "WHERE prod_sell_name_norm LIKE ? AND reg_status = 'Действующий' "
        "ORDER BY prod_sell_name LIMIT 10",
        (like,),
    ).fetchall()
    return jsonify(items=[r["prod_sell_name"] for r in rows])


# --------------------------------------------------- график на день и журнал

def _slot_is_valid(med, times, slot_dt):
    """Плановое время должно быть в ТЕКУЩЕМ графике лекарства на эту дату."""
    day = slot_dt.date().isoformat()
    if day < med["start_date"] or (med["end_date"] and day > med["end_date"]):
        return False
    if not med["days_mask"] & (1 << slot_dt.weekday()):
        return False
    return slot_dt.strftime("%H:%M") in times


def _intake_to_json(r):
    return {
        "id": r["id"],
        "medication_id": r["medication_id"],
        "medication_name": r["medication_name"],
        "dose_value": r["dose_value"],
        "dose_unit": r["dose_unit"],
        "scheduled_at": r["scheduled_at"],
        "status": r["status"],
        "taken_at": r["taken_at"],
        "comment": r["comment"] or "",
    }


@reminders_bp.get("/api/medication-schedule")
@login_required
def api_medication_schedule():
    """График на выбранный день (по умолчанию сегодня) с отметками.
    state: taken / skipped — ЗАПИСАННЫЕ факты (state_basis = "recorded");
           pending / unmarked — РАСЧЁТ по текущему времени (state_basis = "calculated")."""
    try:
        day = parse_iso_date(request.args.get("date"), _now().date())
    except ValueError as e:
        return jsonify(error=str(e)), 400

    day_s = day.isoformat()
    db = get_db()
    metas = db.execute(
        "SELECT * FROM medications WHERE user_id = ? AND deleted_at IS NULL AND is_active = 1 "
        "AND start_date <= ? AND (end_date IS NULL OR end_date >= ?)",
        (session["user_id"], day_s, day_s),
    ).fetchall()
    metas = [m for m in metas if m["days_mask"] & (1 << day.weekday())]
    times = _load_times(db, [m["id"] for m in metas])

    intakes = db.execute(
        "SELECT * FROM medication_intakes WHERE user_id = ? AND deleted_at IS NULL "
        "AND ((scheduled_at >= ? AND scheduled_at < ?) OR (scheduled_at IS NULL AND taken_at >= ? AND taken_at < ?))",
        (session["user_id"], day_s + " 00:00:00", (day + timedelta(days=1)).isoformat() + " 00:00:00",
         day_s + " 00:00:00", (day + timedelta(days=1)).isoformat() + " 00:00:00"),
    ).fetchall()
    by_slot = {(i["medication_id"], i["scheduled_at"]): i for i in intakes if i["scheduled_at"]}

    now = _now()
    slots = []
    for m in metas:
        for t in times.get(m["id"], []):
            scheduled_at = f"{day_s} {t}:00"
            intake = by_slot.get((m["id"], scheduled_at))
            if intake:
                state, basis = intake["status"], "recorded"
            else:
                slot_dt = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M:%S")
                overdue = slot_dt + timedelta(minutes=UNMARKED_AFTER_MIN) <= now
                state, basis = ("unmarked" if overdue else "pending"), "calculated"
            slots.append({
                "medication_id": m["id"],
                "name": m["name"],
                "dose_value": m["dose_value"],
                "dose_unit": m["dose_unit"],
                "instructions": m["instructions"] or "",
                "scheduled_at": scheduled_at,
                "time": t,
                "state": state,
                "state_basis": basis,
                "intake_id": intake["id"] if intake else None,
                "taken_at": intake["taken_at"] if intake else None,
                "comment": (intake["comment"] or "") if intake else "",
            })
    slots.sort(key=lambda s: (s["scheduled_at"], s["name"].lower()))

    unscheduled = [_intake_to_json(i) for i in intakes if i["scheduled_at"] is None]
    return jsonify(date=day_s, slots=slots, unscheduled=unscheduled)


@reminders_bp.post("/api/medication-intakes")
@login_required
def api_intake_create():
    data = _json_body()
    if data is None:
        return _bad_request()
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    cached = get_idempotent_response(session["user_id"], "api_intake_create", idem_key)
    if cached is not None:
        return jsonify(cached)

    db = get_db()
    try:
        try:
            med_id = int(data.get("medication_id"))
        except (TypeError, ValueError):
            raise ValueError("Выберите лекарство")
        med = _get_med(db, med_id)
        if not med:
            return jsonify(error="Лекарство не найдено"), 404

        status = str(data.get("status", "")).strip()
        if status not in ("taken", "skipped"):
            raise ValueError("Статус: принял или пропустил")

        raw_slot = data.get("scheduled_at")
        scheduled_at = None
        if raw_slot is not None and str(raw_slot).strip() != "":
            scheduled_at = parse_dt(raw_slot)
            if not scheduled_at.endswith(":00"):
                raise ValueError("Плановое время: укажите с точностью до минуты")
            slot_dt = datetime.strptime(scheduled_at, "%Y-%m-%d %H:%M:%S")
            times = _load_times(db, [med_id]).get(med_id, [])
            if not _slot_is_valid(med, times, slot_dt):
                raise ValueError("Такого времени приёма нет в графике. Для приёма вне графика не указывайте плановое время")
        elif status == "skipped":
            raise ValueError("Пропуск можно отметить только для приёма по графику")

        taken_at = None
        if status == "taken":
            taken_at = _parse_taken_at(data.get("taken_at"))
        comment = _text(data.get("comment"), 500, "Комментарий")
    except ValueError as e:
        return jsonify(error=str(e)), 400

    if scheduled_at:
        existing = db.execute(
            "SELECT * FROM medication_intakes WHERE medication_id = ? AND scheduled_at = ? AND deleted_at IS NULL",
            (med_id, scheduled_at),
        ).fetchone()
        if existing:
            if existing["status"] == status:
                return jsonify(ok=True, id=existing["id"], duplicate=True)
            return jsonify(error="Для этого приёма уже есть другая отметка. Измените её или удалите"), 409

    try:
        cur = db.execute(
            "INSERT INTO medication_intakes (user_id, medication_id, medication_name, dose_value, dose_unit, "
            "scheduled_at, status, taken_at, comment, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual')",
            (session["user_id"], med_id, med["name"], med["dose_value"], med["dose_unit"],
             scheduled_at, status, taken_at, comment),
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Параллельный повтор того же запроса успел записать отметку раньше.
        db.rollback()
        existing = db.execute(
            "SELECT * FROM medication_intakes WHERE medication_id = ? AND scheduled_at = ? AND deleted_at IS NULL",
            (med_id, scheduled_at),
        ).fetchone()
        if existing and existing["status"] == status:
            return jsonify(ok=True, id=existing["id"], duplicate=True)
        return jsonify(error="Для этого приёма уже есть другая отметка. Измените её или удалите"), 409

    audit("create_medication_intake", "medication_intakes", cur.lastrowid, {"status": status, "scheduled": bool(scheduled_at)})
    result = {"ok": True, "id": cur.lastrowid, "duplicate": False}
    store_idempotent_response(session["user_id"], "api_intake_create", idem_key, "medication_intakes", cur.lastrowid, result)
    return jsonify(result)


def _get_intake(db, intake_id):
    return db.execute(
        "SELECT * FROM medication_intakes WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (intake_id, session["user_id"]),
    ).fetchone()


@reminders_bp.patch("/api/medication-intakes/<int:intake_id>")
@login_required
def api_intake_update(intake_id):
    data = _json_body()
    if data is None:
        return _bad_request()
    db = get_db()
    row = _get_intake(db, intake_id)
    if not row:
        return jsonify(error="Отметка не найдена"), 404

    try:
        status = str(data.get("status", row["status"])).strip()
        if status not in ("taken", "skipped"):
            raise ValueError("Статус: принял или пропустил")
        if status == "skipped" and row["scheduled_at"] is None:
            raise ValueError("Пропуск можно отметить только для приёма по графику")

        if status == "taken":
            # Если время не передано и его не было (переход из «пропустил») — текущее.
            taken_at = _parse_taken_at(data.get("taken_at") or row["taken_at"])
        else:
            taken_at = None
        comment = _text(data["comment"], 500, "Комментарий") if "comment" in data else (row["comment"] or "")
    except ValueError as e:
        return jsonify(error=str(e)), 400

    changed = [k for k, (a, b) in {
        "status": (status, row["status"]),
        "taken_at": (taken_at, row["taken_at"]),
        "comment": (comment, row["comment"] or ""),
    }.items() if a != b]

    db.execute(
        "UPDATE medication_intakes SET status = ?, taken_at = ?, comment = ?, updated_at = datetime('now') WHERE id = ?",
        (status, taken_at, comment, intake_id),
    )
    db.commit()
    audit("update_medication_intake", "medication_intakes", intake_id, {"fields": changed})
    return jsonify(ok=True)


@reminders_bp.delete("/api/medication-intakes/<int:intake_id>")
@login_required
def api_intake_delete(intake_id):
    db = get_db()
    row = _get_intake(db, intake_id)
    if not row:
        return jsonify(error="Отметка не найдена"), 404
    db.execute(
        "UPDATE medication_intakes SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (intake_id,),
    )
    db.commit()
    audit("delete_medication_intake", "medication_intakes", intake_id, {"status": row["status"]})
    return jsonify(ok=True)


@reminders_bp.get("/api/medication-intakes")
@login_required
def api_intakes_history():
    today = _now().date()
    try:
        date_to = parse_iso_date(request.args.get("date_to"), today)
        date_from = parse_iso_date(request.args.get("date_from"), date_to - timedelta(days=29))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if date_from > date_to:
        return jsonify(error="Дата начала позже даты окончания"), 400

    params = [session["user_id"],
              date_from.isoformat() + " 00:00:00",
              (date_to + timedelta(days=1)).isoformat() + " 00:00:00"]
    sql = ("SELECT * FROM medication_intakes WHERE user_id = ? AND deleted_at IS NULL "
           "AND COALESCE(scheduled_at, taken_at) >= ? AND COALESCE(scheduled_at, taken_at) < ?")
    med_id = request.args.get("medication_id")
    if med_id not in (None, ""):
        try:
            params.append(int(med_id))
        except ValueError:
            return jsonify(error="Некорректное лекарство"), 400
        sql += " AND medication_id = ?"
    sql += " ORDER BY COALESCE(scheduled_at, taken_at) DESC, id DESC LIMIT 500"
    rows = get_db().execute(sql, params).fetchall()
    return jsonify(intakes=[_intake_to_json(r) for r in rows])


# ------------------------------------------------------- напоминания об измерениях

def _reminder_to_json(r):
    return {
        "id": r["id"],
        "kind": r["kind"],
        "title": r["title"],
        "time": r["time_of_day"],
        "days": _mask_to_days(r["days_mask"]),
        "is_active": bool(r["is_active"]),
    }


def _merge_reminder(data, cur):
    out = dict(cur) if cur else {
        "kind": None, "title": None, "time_of_day": None,
        "days_mask": ALL_DAYS_MASK, "is_active": 1,
    }
    if "kind" in data:
        kind = str(data["kind"]).strip()
        if kind not in REMINDER_KINDS:
            raise ValueError("Тип напоминания: выберите из списка")
        out["kind"] = kind
    if out["kind"] is None:
        raise ValueError("Тип напоминания: выберите из списка")

    if "title" in data:
        out["title"] = _text(data["title"], 100, "Название")
    if not out["title"]:
        default = REMINDER_KINDS[out["kind"]]
        if not default:
            raise ValueError("Название: заполните поле")
        out["title"] = default

    if "time" in data:
        out["time_of_day"] = _parse_time(data["time"])
    if not out["time_of_day"]:
        raise ValueError("Время: укажите время в формате ЧЧ:ММ")

    if "days" in data:
        out["days_mask"] = _parse_days(data["days"])
    if "is_active" in data:
        out["is_active"] = _parse_bool(data["is_active"], "Активность")
    return out


def _get_reminder(db, reminder_id):
    return db.execute(
        "SELECT * FROM reminders WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
        (reminder_id, session["user_id"]),
    ).fetchone()


@reminders_bp.get("/api/reminders")
@login_required
def api_reminders_list():
    rows = get_db().execute(
        "SELECT * FROM reminders WHERE user_id = ? AND deleted_at IS NULL ORDER BY time_of_day, id",
        (session["user_id"],),
    ).fetchall()
    return jsonify(reminders=[_reminder_to_json(r) for r in rows])


@reminders_bp.post("/api/reminders")
@login_required
def api_reminder_create():
    data = _json_body()
    if data is None:
        return _bad_request()
    idem_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")
    cached = get_idempotent_response(session["user_id"], "api_reminder_create", idem_key)
    if cached is not None:
        return jsonify(cached)
    try:
        f = _merge_reminder(data, None)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    db = get_db()
    count = db.execute(
        "SELECT COUNT(*) AS c FROM reminders WHERE user_id = ? AND deleted_at IS NULL",
        (session["user_id"],),
    ).fetchone()["c"]
    if count >= MAX_REMINDERS:
        return jsonify(error=f"Достигнут предел: {MAX_REMINDERS} напоминаний"), 400

    cur = db.execute(
        "INSERT INTO reminders (user_id, kind, title, time_of_day, days_mask, is_active) VALUES (?, ?, ?, ?, ?, ?)",
        (session["user_id"], f["kind"], f["title"], f["time_of_day"], f["days_mask"], f["is_active"]),
    )
    db.commit()
    audit("create_reminder", "reminders", cur.lastrowid, {"kind": f["kind"]})
    result = {"ok": True, "id": cur.lastrowid}
    store_idempotent_response(session["user_id"], "api_reminder_create", idem_key, "reminders", cur.lastrowid, result)
    return jsonify(result)


@reminders_bp.patch("/api/reminders/<int:reminder_id>")
@login_required
def api_reminder_update(reminder_id):
    data = _json_body()
    if data is None:
        return _bad_request()
    db = get_db()
    row = _get_reminder(db, reminder_id)
    if not row:
        return jsonify(error="Напоминание не найдено"), 404
    try:
        f = _merge_reminder(data, dict(row))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    changed = [k for k in ("kind", "title", "time_of_day", "days_mask", "is_active") if f[k] != row[k]]
    db.execute(
        "UPDATE reminders SET kind = ?, title = ?, time_of_day = ?, days_mask = ?, is_active = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (f["kind"], f["title"], f["time_of_day"], f["days_mask"], f["is_active"], reminder_id),
    )
    db.commit()
    audit("update_reminder", "reminders", reminder_id, {"fields": changed})
    return jsonify(ok=True)


@reminders_bp.delete("/api/reminders/<int:reminder_id>")
@login_required
def api_reminder_delete(reminder_id):
    db = get_db()
    row = _get_reminder(db, reminder_id)
    if not row:
        return jsonify(error="Напоминание не найдено"), 404
    db.execute(
        "UPDATE reminders SET deleted_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        (reminder_id,),
    )
    db.commit()
    audit("delete_reminder", "reminders", reminder_id, {"kind": row["kind"]})
    return jsonify(ok=True)
