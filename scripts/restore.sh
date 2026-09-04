#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ -z "${1:-}" ]; then
  echo "Использование: ./scripts/restore.sh /полный/путь/к/файлу.db" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi

$DC stop app || true
cp "$1" data/medical_diary.db
$DC start app

echo "Восстановление завершено: $1"
