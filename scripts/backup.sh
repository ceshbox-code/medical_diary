#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
else
  DC="docker-compose"
fi

STAMP=$(date +%Y%m%d_%H%M%S)
$DC exec -T app python /app/backup.py "/backups/medical_diary_${STAMP}.db"

find ./backups -type f -name 'medical_diary_*.db' -mtime +30 -delete
