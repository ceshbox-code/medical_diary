"""
Фоновый наблюдатель за директорией с еженедельными выгрузками ЦРПТ
(открытые данные "GTIN -> лекарство", см. import_mdlp_gtins.py).

Как только в MDLP_IMPORT_DIR появляется новый или изменившийся файл вида
data-<дата>-....csv (имя специально не фиксировано жёстко — дата в
названии меняется каждую неделю), он автоматически прогоняется через
import_mdlp_gtins.main(), а результат фиксируется в таблице
mdlp_import_log — чтобы один и тот же файл не импортировался повторно
на каждой следующей проверке.

Использование: просто положить скачанный файл в
/data/mdlp_import/ (или другой путь, если задан MDLP_IMPORT_DIR) —
он подхватится сам при следующей проверке (по умолчанию раз в 5 минут,
см. MDLP_WATCH_INTERVAL_SECONDS). /data уже смонтирован в контейнер на
запись — отдельный volume для этого не нужен.

Это ПОЛЛИНГ (периодическая проверка), а не inotify — сознательный выбор:
не тянет новых системных зависимостей и не требует правок Dockerfile,
достаточно надёжно для файла, который меняется примерно раз в неделю.
"""
import os
import re
import threading
import time
import traceback

from db import get_db

MDLP_IMPORT_DIR = os.getenv("MDLP_IMPORT_DIR", "/data/mdlp_import")
MDLP_WATCH_INTERVAL_SECONDS = int(os.getenv("MDLP_WATCH_INTERVAL_SECONDS", "86400"))

# Специально не завязано на конкретное имя файла целиком (структура-суффикс
# в названии тоже может со временем поменяться) — только на префикс "data-"
# и дату сразу после него, и расширение .csv.
_FILENAME_RE = re.compile(r"^data-\d{8}.*\.csv$", re.IGNORECASE)

_watcher_started = False


def _already_imported(db, filename, size, mtime):
    row = db.execute(
        "SELECT 1 FROM mdlp_import_log WHERE filename = ? AND size = ? AND mtime = ? AND status = 'ok'",
        (filename, size, mtime),
    ).fetchone()
    return row is not None


def _record(db, filename, size, mtime, status, detail):
    db.execute(
        "INSERT INTO mdlp_import_log (filename, size, mtime, status, detail, imported_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(filename, size, mtime) DO UPDATE SET "
        "status = excluded.status, detail = excluded.detail, imported_at = excluded.imported_at",
        (filename, size, mtime, status, detail),
    )
    db.commit()


def _scan_once(app):
    if not os.path.isdir(MDLP_IMPORT_DIR):
        return
    with app.app_context():
        db = get_db()
        try:
            names = sorted(os.listdir(MDLP_IMPORT_DIR))
        except OSError as e:
            print(f"[mdlp-watcher] Не удалось прочитать {MDLP_IMPORT_DIR}: {e}", flush=True)
            return

        for name in names:
            if not _FILENAME_RE.match(name):
                continue
            path = os.path.join(MDLP_IMPORT_DIR, name)
            try:
                st = os.stat(path)
            except OSError:
                continue  # файл мог исчезнуть между listdir и stat — не критично, попробуем в следующий раз
            size, mtime = st.st_size, st.st_mtime
            if _already_imported(db, name, size, mtime):
                continue

            print(f"[mdlp-watcher] Обнаружен новый/изменённый файл: {name} ({size} байт)", flush=True)
            import import_mdlp_gtins as importer  # локальный импорт: модуль сам открывает свою sqlite3-сессию
            try:
                rc = importer.main(path)
                if rc == 0:
                    status, detail = "ok", ""
                    print(f"[mdlp-watcher] Импорт {name} завершён успешно", flush=True)
                else:
                    status, detail = "error", f"main() вернул код {rc}"
                    print(f"[mdlp-watcher] Импорт {name} завершился с кодом {rc}", flush=True)
            except Exception as e:  # noqa: BLE001
                status, detail = "error", f"{type(e).__name__}: {e}"
                print(f"[mdlp-watcher] Ошибка импорта {name}: {detail}", flush=True)
                traceback.print_exc()
            _record(db, name, size, mtime, status, detail)


def start_mdlp_watcher(app):
    """Запускает фоновый поток (один на процесс) — тот же паттерн, что у
    push- и backup-планировщиков в этом проекте."""
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    os.makedirs(MDLP_IMPORT_DIR, exist_ok=True)
    print(
        f"[mdlp-watcher] Слежение за {MDLP_IMPORT_DIR} включено, "
        f"проверка каждые {MDLP_WATCH_INTERVAL_SECONDS} с",
        flush=True,
    )

    def loop():
        while True:
            try:
                _scan_once(app)
            except Exception as e:  # noqa: BLE001
                print(f"[mdlp-watcher] Ошибка проверки каталога: {type(e).__name__}: {e}", flush=True)
            time.sleep(MDLP_WATCH_INTERVAL_SECONDS)

    threading.Thread(target=loop, daemon=True, name="mdlp-watcher").start()
