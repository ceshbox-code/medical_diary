#!/usr/bin/env python3
"""
Импорт официальных открытых данных ЦРПТ «Сведения о лекарственных
препаратах для медицинского применения, подлежащих обязательной
маркировке» в локальную таблицу mdlp_gtins.

Источник (открытые данные, публикуются еженедельно, бесплатно, без
регистрации — НЕ путать с платным BI-сервисом "Датамаркет" на том же
домене):
  https://датамаркет.честныйзнак.рф/bi/opendata/7731376812-MDLPGtins

Использование (внутри контейнера или как отдельный шаг деплоя):
  python3 import_mdlp_gtins.py /path/to/data-YYYYMMDD-structure-20240611.csv

Идемпотентно: UPSERT по gtin, повторный/более новый запуск не создаёт
дублей и просто обновляет данные. Сеть при импорте не используется —
файл скачивается человеком заранее и передаётся скрипту локально.

ВАЖНО про колонку "inn" в исходном файле ЦРПТ: это ИНН (налоговый номер)
организации, зарегистрировавшей препарат в системе маркировки, а НЕ
международное непатентованное наименование (для него есть "prod_name").
Мы её сюда сознательно не импортируем, чтобы не путать в дальнейшем.
"""
import csv
import os
import sys

DATABASE = os.environ.get("DATABASE_PATH", "/data/medical_diary.db")
BATCH_SIZE = 2000
REQUIRED_COLUMNS = {"gtin", "prod_sell_name", "prod_desc", "prod_name",
                    "prod_d_name", "prod_form_name", "gnvlp", "reg_status"}


def _valid_gtin(raw):
    s = (raw or "").strip()
    return s if s.isdigit() and 8 <= len(s) <= 14 else None


def _flush(conn, batch):
    conn.executemany(
        "INSERT INTO mdlp_gtins "
        "(gtin, prod_sell_name, prod_sell_name_norm, prod_desc, prod_name, dose_raw, form_name, gnvlp, reg_status, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(gtin) DO UPDATE SET "
        "prod_sell_name = excluded.prod_sell_name, prod_sell_name_norm = excluded.prod_sell_name_norm, "
        "prod_desc = excluded.prod_desc, "
        "prod_name = excluded.prod_name, dose_raw = excluded.dose_raw, "
        "form_name = excluded.form_name, gnvlp = excluded.gnvlp, "
        "reg_status = excluded.reg_status, updated_at = excluded.updated_at",
        batch,
    )


def main(csv_path):
    import sqlite3

    if not os.path.isfile(csv_path):
        print(f"Файл не найден: {csv_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(DATABASE)
    # На случай запуска до первого старта приложения (init_db ещё не
    # выполнялся) — создаём таблицу и здесь, идентично схеме в db.py.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mdlp_gtins (
          gtin TEXT PRIMARY KEY,
          prod_sell_name TEXT,
          prod_sell_name_norm TEXT,
          prod_desc TEXT,
          prod_name TEXT,
          dose_raw TEXT,
          form_name TEXT,
          gnvlp TEXT,
          reg_status TEXT,
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # На случай запуска против базы, созданной ДО добавления этой колонки
    # (см. db.py: init_db делает такую же проверку через ALTER TABLE).
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(mdlp_gtins)").fetchall()}
    if "prod_sell_name_norm" not in existing_cols:
        conn.execute("ALTER TABLE mdlp_gtins ADD COLUMN prod_sell_name_norm TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mdlp_gtins_name_norm ON mdlp_gtins(prod_sell_name_norm)")

    total = 0
    skipped_bad_gtin = 0
    batch = []

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            print(f"В файле нет ожидаемых колонок: {sorted(missing)}", file=sys.stderr)
            print(f"Найденные колонки: {reader.fieldnames}", file=sys.stderr)
            return 1

        for row in reader:
            gtin = _valid_gtin(row.get("gtin"))
            if not gtin:
                skipped_bad_gtin += 1
                continue
            sell_name = (row.get("prod_sell_name") or "").strip() or None
            batch.append((
                gtin,
                sell_name,
                sell_name.lower() if sell_name else None,  # .lower() в Python корректно сворачивает кириллицу, в отличие от SQL LOWER()
                (row.get("prod_desc") or "").strip() or None,
                (row.get("prod_name") or "").strip() or None,
                (row.get("prod_d_name") or "").strip() or None,
                (row.get("prod_form_name") or "").strip() or None,
                (row.get("gnvlp") or "").strip() or None,
                (row.get("reg_status") or "").strip() or None,
            ))
            if len(batch) >= BATCH_SIZE:
                _flush(conn, batch)
                total += len(batch)
                batch = []

    if batch:
        _flush(conn, batch)
        total += len(batch)

    conn.commit()
    conn.close()
    print(f"Готово: загружено/обновлено {total} GTIN, пропущено некорректных строк {skipped_bad_gtin}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python3 import_mdlp_gtins.py <путь_к_data-*.csv>", file=sys.stderr)
        sys.exit(1)
    sys.exit(main(sys.argv[1]))
