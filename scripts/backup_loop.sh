#!/bin/sh
set -eu

: "${BACKUP_INTERVAL_SECONDS:=86400}"

while true; do
  /scripts/backup_mysql.sh || echo "Backup failed at $(date)" >&2
  sleep "${BACKUP_INTERVAL_SECONDS}"
done
