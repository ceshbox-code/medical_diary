#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi

# Без -T: интерактивный ввод пароля (--ask) и подтверждение (y/n) должны
# идти через настоящий TTY. Контейнер при этом продолжает обслуживать
# обычные запросы — останавливать его не нужно.
$DC exec app python /app/reset_admin_password.py "$@"
