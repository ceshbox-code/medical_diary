"""Резервное копирование и восстановление SQLite-базы.

Извлечено из app.py на шаге 5 модуляризации.

ВАЖНО (инвариант, не менять порядок без явной причины): в
restore_database_from_backup() аварийная копия текущей БД снимается
НАПРЯМУЮ через sqlite3.Connection.backup(), а не через backup_database() —
та использует fcntl-блокировку, и вызов backup_database() изнутри уже
удерживаемой блокировки приводил к дедлоку (баг был найден и исправлен
ранее). Функция запуска планировщика (`if BACKUP_ENABLED: ...
threading.Thread(...).start()`) остаётся в app.py — это осознанное решение,
чтобы порядок side-effect'ов при старте приложения оставался виден в одном
месте, как и вызов init_db().
"""

import os
import sqlite3
import glob
import fcntl
import secrets
import threading
import time
from datetime import datetime, timedelta

from flask import g

from db import DATABASE, init_db


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

