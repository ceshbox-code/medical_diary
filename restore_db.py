#!/usr/bin/env python3
"""Безопасное восстановление Medical Diary из SQLite-резервной копии.

Перед восстановлением автоматически создаётся аварийная копия текущей БД.
Исходная резервная копия не изменяется.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import datetime


def sqlite_backup(src_path: str, dst_path: str) -> None:
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def integrity_ok(path: str) -> bool:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Восстановление Medical Diary из SQLite backup")
    parser.add_argument("backup", help="Путь к резервной копии .db")
    parser.add_argument("--database", default=os.getenv("DATABASE_PATH", "/data/medical_diary.db"))
    parser.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    args = parser.parse_args()

    database = os.path.abspath(args.database)
    backup = os.path.abspath(args.backup)

    if not os.path.isfile(backup):
        print(f"Ошибка: резервная копия не найдена: {backup}", file=sys.stderr)
        return 2
    if not backup.lower().endswith(".db"):
        print("Ошибка: разрешены только файлы .db", file=sys.stderr)
        return 2
    if os.path.exists(database) and os.path.samefile(backup, database):
        print("Ошибка: резервная копия совпадает с текущей БД", file=sys.stderr)
        return 2
    if not integrity_ok(backup):
        print("Ошибка: резервная копия не прошла SQLite integrity_check", file=sys.stderr)
        return 3

    if not args.yes:
        answer = input(
            f"Восстановить БД {database} из {backup}? "
            "Текущая БД будет сохранена в аварийную копию. [yes/NO]: "
        ).strip().lower()
        if answer not in ("yes", "y", "да"):
            print("Отменено.")
            return 0

    os.makedirs(os.path.dirname(database) or ".", exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    emergency = database + f".pre-restore-{stamp}.db"
    temp = database + f".restore-{stamp}.tmp"

    try:
        if os.path.exists(database):
            sqlite_backup(database, emergency)
            if not integrity_ok(emergency):
                raise RuntimeError("аварийная копия текущей БД не прошла integrity_check")
            print(f"Аварийная копия: {emergency}")

        sqlite_backup(backup, temp)
        if not integrity_ok(temp):
            raise RuntimeError("восстановленная временная БД не прошла integrity_check")

        # Убираем WAL/SHM старой базы только после закрытия соединений,
        # чтобы новый файл не оказался связан со старым WAL-журналом.
        for sidecar in (database + "-wal", database + "-shm"):
            try:
                os.remove(sidecar)
            except FileNotFoundError:
                pass

        os.replace(temp, database)
        print(f"БД восстановлена: {database}")
        return 0
    except Exception as exc:
        print(f"Ошибка восстановления: {exc}", file=sys.stderr)
        try:
            os.remove(temp)
        except FileNotFoundError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
