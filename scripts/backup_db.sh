#!/bin/sh
# Резервная копия БД с ротацией (ТЗ 6.1, п.7).
# Запуск вручную:  sh scripts/backup_db.sh
# По расписанию:   сервис backup в docker-compose.yml
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
PGHOST="${POSTGRES_HOST:-db}"
PGUSER="${POSTGRES_USER:-hwls}"
PGDATABASE="${POSTGRES_DB:-hwls}"
export PGPASSWORD="${POSTGRES_PASSWORD:-hwls}"

mkdir -p "$BACKUP_DIR"
STAMP=$(date -u +%Y%m%d-%H%M%S)
TARGET="$BACKUP_DIR/hwls-$STAMP.sql.gz"

echo "[backup] $PGDATABASE@$PGHOST -> $TARGET"
pg_dump -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists | gzip -9 > "$TARGET.part"
mv "$TARGET.part" "$TARGET"

# Пустой или подозрительно маленький дамп — повод для тревоги, а не тихий успех
SIZE=$(wc -c < "$TARGET")
if [ "$SIZE" -lt 1024 ]; then
    echo "[backup] ОШИБКА: дамп подозрительно мал ($SIZE байт)" >&2
    exit 1
fi

find "$BACKUP_DIR" -name 'hwls-*.sql.gz' -mtime "+$KEEP_DAYS" -delete
echo "[backup] готово: $TARGET ($SIZE байт), храним $KEEP_DAYS дней"
