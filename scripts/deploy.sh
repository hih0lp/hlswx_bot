#!/usr/bin/env bash
# Деплой основного бота (HWLS) на сервер по ключевому SSH.
#
#   sh scripts/deploy.sh            обычный деплой
#   CHECK_ONLY=1 sh scripts/deploy.sh   только проверки, без изменений
#
# Работает исключительно в $REMOTE_DIR. Соседние проекты на сервере
# (/opt/hammer-bot, /opt/posting) не затрагиваются — их состояние только
# показывается в конце, чтобы убедиться, что деплой их не задел.
set -euo pipefail

HOST="${DEPLOY_HOST:-161.104.47.79}"
SSH_USER="${DEPLOY_USER:-root}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-/opt/hwls-bot}"
SSH_KEY="${DEPLOY_SSH_KEY:-$HOME/.ssh/id_ed25519}"
LOCAL_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CHECK_ONLY="${CHECK_ONLY:-}"

# Соединение с сервером периодически обрывается на этапе баннера, поэтому
# держим одно мультиплексированное подключение на весь деплой и повторяем
# первую попытку — иначе сетевой сбой оборвёт деплой на середине.
# ConnectTimeout большой намеренно: sshd на этом сервере отдаёт баннер с
# задержкой до ~20 с, и при стандартном таймауте соединение рвётся случайным
# образом посреди деплоя.
CTL="${TMPDIR:-/tmp}/hwls-deploy-%r@%h:%p"
SSH_OPTS="-i $SSH_KEY -o BatchMode=yes -o ConnectTimeout=120 -o StrictHostKeyChecking=accept-new \
-o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
-o ControlMaster=auto -o ControlPath=$CTL -o ControlPersist=10m"
TARGET="$SSH_USER@$HOST"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
rsh() { ssh $SSH_OPTS "$TARGET" "$@"; }
dc() { rsh "cd $REMOTE_DIR && docker compose $*"; }

cleanup() { ssh $SSH_OPTS -O exit "$TARGET" 2>/dev/null || true; }
trap cleanup EXIT

# SQL подаём через stdin: иначе кавычки внутри запроса пришлось бы экранировать
# через четыре слоя (локальный shell → ssh → sh на сервере → docker exec).
# Пользователь и база берутся из окружения контейнера, а не с хоста.
sql() {
    printf '%s\n' "$1" | rsh "cd $REMOTE_DIR && docker compose exec -T db \
sh -c 'psql -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -tA'"
}

# ---------------------------------------------------------------- проверки

say "Проверка доступа"
for attempt in 1 2 3 4 5; do
    if rsh 'hostname && docker --version && docker compose version | head -1'; then
        break
    fi
    [ "$attempt" = 5 ] && { echo "Не удалось подключиться к $TARGET" >&2; exit 1; }
    echo "попытка $attempt не удалась, повтор через 10 с"
    sleep 10
done
rsh "test -f $REMOTE_DIR/docker-compose.yml && test -f $REMOTE_DIR/.env" \
    || { echo "На сервере нет $REMOTE_DIR/docker-compose.yml или .env" >&2; exit 1; }

say "Текущее состояние"
dc ps
sql "select status, count(*) from publish_jobs group by status order by 1" || true

# Задачи, поставленные старым кодом, не привязаны к publications: они
# опубликуются, но уведомления о статусе по ним не придёт. Лучше деплоить,
# когда очередь пуста.
QUEUED=$(sql "select count(*) from publish_jobs where status = 'queued'" 2>/dev/null | tr -d '[:space:]' || echo 0)
if [ "${QUEUED:-0}" != "0" ]; then
    echo "ВНИМАНИЕ: в очереди $QUEUED задач от старого кода — по ним не будет уведомлений о статусе."
fi

if [ -n "$CHECK_ONLY" ]; then
    say "CHECK_ONLY — изменения не вносились"
    exit 0
fi

# ---------------------------------------------------------------- бэкап

say "Резервная копия БД перед миграцией схемы"
STAMP=$(date -u +%Y%m%d-%H%M%S)
DUMP="/root/hwls-pre-deploy-$STAMP.sql.gz"
rsh "cd $REMOTE_DIR && docker compose exec -T db sh -c 'PGPASSWORD=\"\$POSTGRES_PASSWORD\" pg_dump -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" --clean --if-exists' | gzip -9 > $DUMP"
rsh "ls -lh $DUMP && test \$(wc -c < $DUMP) -gt 1024"

say "Метка текущего образа для отката"
IMAGE=$(dc images -q app | head -1 | tr -d '[:space:]' || true)
if [ -n "${IMAGE:-}" ]; then
    rsh "docker image tag $IMAGE hwls-app:pre-deploy-$STAMP"
    echo "Откат: docker image tag hwls-app:pre-deploy-$STAMP <текущий тег> && docker compose up -d --force-recreate app"
else
    echo "Образ приложения не найден — вероятно, первый деплой."
fi

# ---------------------------------------------------------------- код

say "Синхронизация кода"
# .env сервера не трогаем; /models/ и /data/ в корне проекта хранят обученную
# модель и датасет, которых нет локально.
# Ведущий слэш обязателен: шаблон 'models' совпал бы и с пакетом app/models/,
# и на сервер уехал бы новый seed.py со старыми моделями.
rsync -az --info=stats1 -e "ssh $SSH_OPTS" \
    --exclude '.git' --exclude '.env' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.venv' \
    --exclude '/models/' --exclude '/data/' \
    --exclude '/assets/welcome_file_id.txt' \
    --exclude '/assets/banner_ids.json' \
    --exclude '/docs/design/' --exclude '*.docx' \
    --exclude 'screen.png' --exclude 'fixes*.jpeg' --exclude 'fixes*.jpg' \
    "$LOCAL_ROOT/" "$TARGET:$REMOTE_DIR/"
rsh "chmod +x $REMOTE_DIR/scripts/entrypoint.sh $REMOTE_DIR/scripts/*.sh"

# Проверка, что код действительно доехал: несовпадение seed.py и моделей
# роняет миграцию схемы на старте.
rsh "cd $REMOTE_DIR && grep -q 'class Publication' app/models/entities.py \
  && grep -q 'class OneshotEvent' app/models/entities.py \
  && grep -q 'class PublishEvent' app/models/entities.py" \
  || { echo "app/models/entities.py на сервере без новых моделей — синхронизация неполная" >&2; exit 1; }

say "Настройки очереди в .env"
# Дописываем только отсутствующие ключи — существующие значения не трогаем.
# DIRECT_PUBLISH_FALLBACK выключен намеренно: пока не подтверждены права бота
# на публикацию во всех группах, ошибка прав из fallback-попытки считается
# постоянной и отменяет задачу без ретраев.
rsh "cd $REMOTE_DIR && for kv in \
      'QUEUE_SILENCE_MINUTES=20' \
      'QUEUE_USER_PAUSE_SEC=120' \
      'QUEUE_JOB_TTL_MINUTES=120' \
      'DIRECT_PUBLISH_FALLBACK=false' \
      'EDIT_SKIPS_FRAUD_CHECK=true' \
      'HAMMER_RELAY_EDIT_ENABLED=false' \
      'BACKUP_KEEP_DAYS=14'; do \
        key=\${kv%%=*}; \
        grep -q \"^\$key=\" .env || { echo \"\$kv\" >> .env; echo \"добавлено: \$kv\"; }; \
      done; grep -E '^(QUEUE_|DIRECT_PUBLISH|EDIT_SKIPS|HAMMER_RELAY_EDIT|BACKUP_KEEP)' .env"

# Старые имена с нестандартными значениями молча потеряют смысл — предупреждаем.
rsh "cd $REMOTE_DIR && grep -E '^QUEUE_(PACKAGE_LOCK_MINUTES|SUBSCRIPTION_INTERVAL_SEC)=' .env || true"

# ---------------------------------------------------------------- запуск

say "Сборка и запуск (db и redis не перезапускаются)"
# Схема обновляется автоматически: entrypoint запускает app.core.seed,
# который делает create_all + идемпотентные ALTER ... IF NOT EXISTS.
dc up -d --build

say "Ожидание готовности"
for i in $(seq 1 30); do
    if rsh "curl -fsS -m 5 http://127.0.0.1:8082/health" >/dev/null 2>&1; then
        echo "health ok через $((i * 5)) с"
        break
    fi
    rsh 'sleep 5'
    [ "$i" = 30 ] && { echo "Приложение не поднялось за 150 с" >&2; rsh 'docker logs hwls_app --tail 80'; exit 1; }
done

# ---------------------------------------------------------------- проверка

say "Состояние сервисов"
dc ps

say "Логи приложения"
rsh 'docker logs hwls_app --tail 40 2>&1'

say "Схема: таблицы админ-панели (этап 2)"
sql "select table_name from information_schema.tables
      where table_name in ('admin_access','app_settings','tariff_categories','admin_audit_log') order by 1"
sql "select column_name from information_schema.columns
      where table_name = 'whitelist_entries' and column_name = 'expires_at'"

say "Схема: отметка последней активности пользователя (раздел «Пользователи»)"
sql "select column_name, is_nullable from information_schema.columns
      where table_name = 'users' and column_name = 'last_seen_at'"

say "Схема: новые таблицы очереди"
sql "select table_name from information_schema.tables
      where table_name in ('publications','oneshot_events','publish_events') order by 1"
sql "select column_name from information_schema.columns
      where table_name = 'publish_jobs'
        and column_name in ('publication_id','author_user_id','expires_at','delivery') order by 1"

say "Резервное копирование работает"
dc exec -T backup sh /scripts/backup_db.sh
dc exec -T backup sh /scripts/restore_check.sh

say "Health"
rsh 'curl -sS -m 10 http://127.0.0.1:8082/health; echo'

say "Соседние проекты (только просмотр)"
rsh 'cd /opt/hammer-bot && docker compose ps 2>&1 | tail -5' || true
rsh 'cd /opt/posting/app && docker compose -f docker-compose.prod.yml ps 2>&1 | tail -5' || true

say "Готово. Дамп до деплоя: $DUMP"
