#!/usr/bin/env python3
"""Восстановление доступа: сброс пароля пользователя (по умолчанию —
администратора) напрямую в SQLite, без запущенного HTTP-сервера и без
входа в систему.

Зачем это нужно
----------------
`install_synology.sh` генерирует ADMIN_PASSWORD только один раз — при
самом первом создании .env (пустая БД). После этого `db.py: init_db()`
пытается создать пользователя ADMIN_USERNAME повторным INSERT, который
падает на UNIQUE(username) и молча игнорируется (`except
sqlite3.IntegrityError: pass`) — то есть правка ADMIN_PASSWORD в .env
на уже работающей установке ни на что не влияет. Единственный штатный
способ сменить пароль — уже быть залогиненным администратором и
использовать /api/admin/users/<id> (PATCH) из панели. Если доступ
потерян (забыт пароль, нет активной сессии) — штатного пути не было.
Этот скрипт закрывает именно этот пробел.

Запуск (внутри контейнера, где есть werkzeug — та же библиотека и тот
же формат хэша, что использует app.py при проверке пароля):

    docker compose exec app python /app/reset_admin_password.py --ask
    docker compose exec app python /app/reset_admin_password.py
    docker compose exec app python /app/reset_admin_password.py --create --ask

Или через обёртку с хоста: ./scripts/reset_admin_password.sh --ask

Скрипт НЕ требует остановки контейнера: меняется одна строка в БД в
рамках обычной SQLite-транзакции (WAL допускает это при работающем
приложении), файл БД целиком не подменяется — в отличие от
restore_db.py, здесь останавливать `app` не нужно.
"""

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime


# Тот же путь по умолчанию, что и в db.py (DATABASE_PATH), и та же
# переменная для имени администратора, что и в install_synology.sh /
# db.py (ADMIN_USERNAME) — намеренно переиспользуем оба, чтобы скрипт
# без аргументов по умолчанию работал с тем же аккаунтом.
DEFAULT_DATABASE = os.getenv("DATABASE_PATH", "/data/medical_diary.db")
DEFAULT_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

MIN_PASSWORD_LENGTH = 8  # то же правило, что в app.py (admin_create_user/admin_update_user)
GENERATED_PASSWORD_LENGTH = 24


def generate_password(length: int = GENERATED_PASSWORD_LENGTH) -> str:
    # Тот же принцип, что в install_synology.sh (openssl rand + фильтр
    # алфавита), только средствами Python: secrets.choice — криптостойкий
    # источник случайности из стандартной библиотеки.
    alphabet = string.ascii_letters + string.digits + "@#%+=_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def read_password_interactively() -> str:
    while True:
        pw1 = getpass.getpass("Новый пароль: ")
        if len(pw1) < MIN_PASSWORD_LENGTH:
            print(f"Пароль слишком короткий (минимум {MIN_PASSWORD_LENGTH} символов).", file=sys.stderr)
            continue
        pw2 = getpass.getpass("Повторите пароль: ")
        if pw1 != pw2:
            print("Пароли не совпадают, попробуйте снова.", file=sys.stderr)
            continue
        return pw1


def confirm(prompt: str) -> bool:
    answer = input(f"{prompt} [yes/NO]: ").strip().lower()
    return answer in ("yes", "y", "да")


def connect(database: str) -> sqlite3.Connection:
    if not os.path.isfile(database):
        print(f"Ошибка: файл базы данных не найден: {database}", file=sys.stderr)
        sys.exit(2)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def write_audit(conn: sqlite3.Connection, user_id: int, action: str, details: dict) -> None:
    # Пишем в ту же таблицу audit_log, что и security.py: audit() — но
    # без Flask (нет session/request), поэтому ip_address/user_agent
    # заполняем явными маркерами "это был offline-CLI", а не оставляем
    # NULL молча, чтобы при разборе журнала это не выглядело как вход
    # без источника.
    try:
        conn.execute(
            "INSERT INTO audit_log (user_id, action, entity_type, entity_id, ip_address, user_agent, details_json) "
            "VALUES (?, ?, 'users', ?, ?, ?, ?)",
            (
                user_id,
                action,
                user_id,
                "cli:local",
                "cli:reset_admin_password.py",
                json.dumps(details, ensure_ascii=False),
            ),
        )
        conn.commit()
    except Exception as exc:  # аудит не должен блокировать восстановление доступа
        print(f"Предупреждение: не удалось записать audit_log: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Сброс пароля пользователя (по умолчанию — администратора) напрямую в БД."
    )
    parser.add_argument("--database", default=DEFAULT_DATABASE, help=f"Путь к файлу БД (по умолчанию: {DEFAULT_DATABASE})")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help=f"Логин пользователя (по умолчанию: {DEFAULT_USERNAME}, из ADMIN_USERNAME)")

    pw_group = parser.add_mutually_exclusive_group()
    pw_group.add_argument("--ask", action="store_true", help="Ввести новый пароль интерактивно (не отображается на экране, не попадает в историю shell) — рекомендуемый способ")
    pw_group.add_argument("--password", help="Задать пароль явным аргументом. ВНИМАНИЕ: осядет в истории команд shell и в списке процессов — используйте --ask, где возможно")

    parser.add_argument("--create", action="store_true", help="Создать пользователя, если он не найден (иначе скрипт завершится с ошибкой)")
    parser.add_argument("--reactivate", action="store_true", help="Дополнительно перевести аккаунт в статус 'active', если он был деактивирован")
    parser.add_argument("--grant-admin", action="store_true", help="Дополнительно выдать права администратора (is_admin=1)")
    parser.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    args = parser.parse_args()

    if args.password is not None and len(args.password) < MIN_PASSWORD_LENGTH:
        print(f"Ошибка: пароль слишком короткий (минимум {MIN_PASSWORD_LENGTH} символов).", file=sys.stderr)
        return 2

    if len(args.username.strip()) < 3 or len(args.username.strip()) > 64:
        print("Ошибка: логин должен быть от 3 до 64 символов (как в панели администратора).", file=sys.stderr)
        return 2

    try:
        from werkzeug.security import generate_password_hash
    except ImportError:
        print(
            "Ошибка: модуль werkzeug не найден. Этот скрипт нужно запускать ВНУТРИ контейнера "
            "приложения (там же, где установлены зависимости app.py), например:\n"
            "  docker compose exec app python /app/reset_admin_password.py --ask",
            file=sys.stderr,
        )
        return 2

    conn = connect(os.path.abspath(args.database))
    username = args.username.strip()

    try:
        row = conn.execute(
            "SELECT id, status, is_admin FROM users WHERE username = ?", (username,)
        ).fetchone()

        generated = False
        if args.ask:
            password = read_password_interactively()
        elif args.password is not None:
            password = args.password
            print(
                "Предупреждение: пароль передан аргументом командной строки — он мог сохраниться "
                "в истории shell на этом хосте. Рекомендуется затем сменить его ещё раз через "
                "профиль администратора в приложении.",
                file=sys.stderr,
            )
        else:
            password = generate_password()
            generated = True

        if row is None:
            if not args.create:
                print(
                    f"Ошибка: пользователь '{username}' не найден. "
                    "Если нужно создать его заново, повторите с флагом --create.",
                    file=sys.stderr,
                )
                return 3

            if not args.yes and not confirm(
                f"Пользователь '{username}' не найден. Создать нового администратора с этим логином?"
            ):
                print("Отменено.")
                return 0

            cur = conn.execute(
                "INSERT INTO users (username, password_hash, display_name, status, is_admin) "
                "VALUES (?, ?, ?, 'active', ?)",
                (username, generate_password_hash(password), username, 1 if (args.grant_admin or username == DEFAULT_USERNAME) else 0),
            )
            conn.commit()
            user_id = cur.lastrowid
            write_audit(conn, user_id, "admin_password_reset_cli_create", {"username": username})
            print(f"Пользователь '{username}' создан (id={user_id}).")
        else:
            user_id = row["id"]
            status_note = ""
            if row["status"] != "active" and not args.reactivate:
                status_note = (
                    f" ВНИМАНИЕ: аккаунт сейчас в статусе '{row['status']}' — пароль будет обновлён, "
                    "но вход останется заблокирован. Добавьте --reactivate, чтобы также вернуть статус 'active'."
                )

            if not args.yes and not confirm(
                f"Сбросить пароль пользователя '{username}' (id={user_id})?{status_note}"
            ):
                print("Отменено.")
                return 0

            if args.reactivate:
                conn.execute(
                    "UPDATE users SET password_hash = ?, status = 'active', updated_at = datetime('now') WHERE id = ?",
                    (generate_password_hash(password), user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
                    (generate_password_hash(password), user_id),
                )

            if args.grant_admin and not row["is_admin"]:
                conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))

            conn.commit()
            write_audit(
                conn,
                user_id,
                "admin_password_reset_cli",
                {
                    "username": username,
                    "reactivated": bool(args.reactivate),
                    "granted_admin": bool(args.grant_admin),
                },
            )
            print(f"Пароль пользователя '{username}' (id={user_id}) обновлён.")
            if status_note:
                print(status_note.strip())

        if generated:
            print("\n--------------------------------------------------")
            print(f"Новый пароль (сохраните его — больше он нигде не выводится):\n{password}")
            print("--------------------------------------------------")

        return 0
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        print(f"Ошибка целостности БД: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        conn.rollback()
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
