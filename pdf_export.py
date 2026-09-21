"""Формирование PDF-отчёта дневника (reportlab) + выборка записей.

Извлечено из app.py на шаге 5 модуляризации.

query_entries() используется НЕ только этим модулем — тот же код
обслуживает JSON-эндпоинт /api/history (список записей на экране),
поэтому при изменении логики фильтрации/сортировки нужно проверять оба
сценария использования, не только PDF-экспорт.
"""

import os
from io import BytesIO
from datetime import date, timedelta
from xml.sax.saxutils import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle

from db import get_db
from validators import (
    DEFAULT_RANGES,
    TEMPERATURE_HIGH_ALERT_C,
    UNIT_RU,
    unit_ru,
    parse_iso_date,
    format_dt_ru,
    status_of,
)
from assessments import _normalize_ai_text


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

STATUS_COLORS = {"low": "#fff3c4", "ok": "#d9f2d9", "high": "#fbd9d9"}
STATUS_ICON = {"low": "\u25bc", "ok": "", "high": "\u25b2"}  # ▼ ниже / (без иконки) норма / ▲ выше


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

        temp_low, temp_high = ranges.get("temperature", DEFAULT_RANGES["temperature"])

        for r in rows:
            val = float(r["temperature_c"])
            # Тот же порядок проверок, что и в assessments.temperature_assessment:
            # от 38.0 °C всегда «выше», иначе — по персональному диапазону.
            st = "high" if val >= TEMPERATURE_HIGH_ALERT_C else status_of(val, temp_low, temp_high)
            icon = STATUS_ICON[st]
            entries.append(
                {
                    "id": r["id"],
                    "type": "temperature",
                    "type_label": "Температура",
                    "measured_at": r["measured_at"],
                    "measured_at_ru": format_dt_ru(r["measured_at"]),
                    "display": f"{val:.1f} °C",
                    "display_html": f'<span class="st-{st}">{val:.1f}{icon}</span> °C',
                    "display_pdf": f'<font backcolor="{STATUS_COLORS[st]}">{val:.1f}{icon}</font> °C',
                    "comment": r["comment"] or "",
                    "sort_value": val,
                    "temperature_c": val,
                    "status": st,
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
