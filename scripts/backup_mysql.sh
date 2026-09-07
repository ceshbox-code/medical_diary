#!/bin/sh
set -eu

umask 077

: "${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}"
: "${MYSQL_DATABASE:=medical_diary}"
: "${BACKUP_RETENTION_DAYS:=30}"

timestamp="$(date +%Y%m%d_%H%M%S)"
backup_file="/backups/${MYSQL_DATABASE}_${timestamp}.sql.gz"

export MYSQL_PWD="${MYSQL_ROOT_PASSWORD}"

mysqldump \
  -h db \
  -u root \
  --single-transaction \
  --quick \
  --lock-tables=false \
  --routines \
  --triggers \
  --events \
  --default-character-set=utf8mb4 \
  "${MYSQL_DATABASE}" | gzip > "${backup_file}"

unset MYSQL_PWD

find /backups -type f -name "${MYSQL_DATABASE}_*.sql.gz" -mtime +"${BACKUP_RETENTION_DAYS}" -delete

echo "Backup created: ${backup_file}"
