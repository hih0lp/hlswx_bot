#!/bin/sh
# Проверка восстановления резервной копии (ТЗ 6.1: бэкапы с проверкой восстановления).
# Разворачивает последний дамп во временную БД и убеждается, что данные на месте.
#   sh scripts/restore_check.sh [путь-к-дампу]
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
PGHOST="${POSTGRES_HOST:-db}"
PGUSER="${POSTGRES_USER:-hwls}"
export PGPASSWORD="${POSTGRES_PASSWORD:-hwls}"

DUMP="${1:-$(ls -1t "$BACKUP_DIR"/hwls-*.sql.gz 2>/dev/null | head -1)}"
if [ -z "${DUMP:-}" ] || [ ! -f "$DUMP" ]; then
    echo "[restore-check] не найден дамп в $BACKUP_DIR" >&2
    exit 1
fi

CHECK_DB="hwls_restore_check_$(date -u +%s)"
echo "[restore-check] $DUMP -> $CHECK_DB"

psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "CREATE DATABASE $CHECK_DB;" >/dev/null
trap 'psql -h "$PGHOST" -U "$PGUSER" -d postgres -c "DROP DATABASE IF EXISTS $CHECK_DB;" >/dev/null 2>&1' EXIT

gunzip -c "$DUMP" | psql -h "$PGHOST" -U "$PGUSER" -d "$CHECK_DB" -q >/dev/null

# Ключевые таблицы очереди должны существовать и читаться
for table in users chats publications publish_jobs chat_publish_locks; do
    COUNT=$(psql -h "$PGHOST" -U "$PGUSER" -d "$CHECK_DB" -tAc "SELECT count(*) FROM $table;" 2>/dev/null || echo "ERR")
    if [ "$COUNT" = "ERR" ]; then
        echo "[restore-check] ОШИБКА: таблица $table не восстановилась" >&2
        exit 1
    fi
    echo "[restore-check]   $table: $COUNT строк"
done

echo "[restore-check] восстановление проверено успешно"
