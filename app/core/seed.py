import asyncio
import logging

from sqlalchemy import text

from app.config import get_settings
from app.db.base import Base
from app.db.session import SessionLocal, engine

logger = logging.getLogger("seed")

# Перечень групп для публикации — прислан заказчиком 14.09.2026 и заменил
# список, доставшийся с этапа 1. Порядок строк задаёт `sort_order`.
#
# Шестое поле — username, под которым чат жил в базе раньше. Оно нужно двум
# строкам: у Ярославля и Новосибирска сменился сам telegram id, и без этой
# подсказки сид не узнал бы старую строку и завёл бы вторую — а действующие
# подписки остались бы привязаны к мёртвому чату. Остальные переименованные
# группы узнаются по совпадающему id.
CHAT_SEED = [
    # city_key, topic, username, chat_id, title, legacy_username
    ("msk", "Работа", "Work77Hammer", -1003031021884, "Работа Москва", None),
    ("msk", "Вакансии", "Vacanciesmskw", -1002790885565, "Вакансии Москва", None),
    ("msk", "Подработка", "Podrabotkamskw", -1002823870082, "Подработка Москва", None),
    ("msk", "Шабашка", "HalturamskHammer", -1002321186753, "Шабашка Москва", None),
    ("msk", "Грузчики", "GruzchikiMskHammer", -1003043294870, "Грузчики Москва", None),
    ("spb", "Работа", "Work78Hammer", -1003107517564, "Работа СПб", None),
    ("spb", "Работа", "Rabota_Spb_W", -1003093804011, "Работа СПб", None),
    ("spb", "Подработка", "Podrabotkaspbw", -1003546637737, "Подработка СПб", None),
    ("spb", "Шабашка", "HalturaSpbHammer", -1004365820111, "Шабашка СПб", None),
    # Сверено с Telegram 20.09.2026: id обеих групп живые, а username в перечне
    # заказчика устарел — «Work52Hammer» группа сменила на «worknnhammer». Релей
    # получает username в теле запроса, поэтому расхождение пишем здесь.
    ("nn", "Работа", "worknnhammer", -1002597006697, "Работа НН", "Work52Hammer"),
    ("nn", "Подработка", "podrabotkannw", -1002717822876, "Подработка НН", None),
    ("smr", "Шабашка", "HalturaSmrHammer", -1003982913780, "Шабашка Самара", None),
    ("smr", "Грузчики", "GruzSmrHammer", -1002537169937, "Грузчики Самара", None),
    ("smr", "Подработка", "Podrabotkasmrw", -1003901112790, "Подработка Самара", None),
    ("nsk", "Подработка", "Podrabotkanskw", -1001692649674, "Подработка Новосибирск", "Rabota_Nsk_W"),
    ("yar", "Грузчики", "Gruzyarhammer", -1004307574523, "Грузчики Ярославль", "Rabota_Yar_W"),
    ("ekb", "Грузчики", "GruzekbHammer", -1003750815837, "Грузчики Екатеринбург", None),
    ("kzn", "Вакансии", "VacancieKzn", -1003917161713, "Вакансии Казань", None),
    ("kld", "Работа", "Work39Hammer", -1003460891609, "Работа Калининград", None),
    ("chel", "Грузчики", "GruzChelHammer", -1003082593004, "Грузчики Челябинск", None),
]


async def wait_db(retries: int = 30, delay: float = 2.0) -> None:
    for attempt in range(1, retries + 1):
        try:
            async with engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception:
            logger.warning("DB not ready (%s/%s)", attempt, retries)
            await asyncio.sleep(delay)
    raise RuntimeError("Database not available")


async def ensure_schema() -> None:
    """Лёгкие обновления схемы без Alembic."""
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS yookassa_account_id VARCHAR(64)"),
        )
        await conn.execute(
            text("ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"),
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS balance NUMERIC(12, 2) NOT NULL DEFAULT 0",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS posts_volume VARCHAR(16) NOT NULL DEFAULT 'low'",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelist_entries ADD COLUMN IF NOT EXISTS username VARCHAR(64)",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelist_entries ADD COLUMN IF NOT EXISTS chat_ids TEXT NOT NULL DEFAULT '[]'",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS photo_url TEXT",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS failed_at TIMESTAMPTZ",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider VARCHAR(20) NOT NULL DEFAULT 'yookassa'",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS plan_type VARCHAR(20) NOT NULL DEFAULT 'standard'",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS bot_token TEXT",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS brand_title VARCHAR(128)",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS linked_user_id INTEGER REFERENCES users(id)",
            ),
        )
        for stmt in (
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_tier VARCHAR(16) NOT NULL DEFAULT 'basic'",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_billing_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_payment_method_id VARCHAR(64)",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_next_charge_at TIMESTAMPTZ",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_grace_until TIMESTAMPTZ",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS franchise_last_notice_at TIMESTAMPTZ",
        ):
            await conn.execute(text(stmt))
        await conn.execute(
            text(
                "UPDATE subscriptions SET plan_type = 'corp_b2b' "
                "WHERE id IN (SELECT subscription_id FROM b2b_integrations WHERE subscription_id IS NOT NULL) "
                "AND plan_type = 'standard'",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE whitelist_entries ALTER COLUMN telegram_id DROP NOT NULL",
            ),
        )
        await conn.execute(
            text(
                "ALTER TABLE chats ADD COLUMN IF NOT EXISTS network VARCHAR(20) NOT NULL DEFAULT 'hammer'",
            ),
        )
        # Очередь публикаций по ТЗ 6.2–6.5
        for stmt in (
            "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS publication_id INTEGER "
            "REFERENCES publications(id) ON DELETE CASCADE",
            "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS author_user_id INTEGER REFERENCES users(id)",
            "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            "ALTER TABLE publish_jobs ADD COLUMN IF NOT EXISTS delivery VARCHAR(10)",
            "CREATE INDEX IF NOT EXISTS ix_publish_jobs_publication_id ON publish_jobs (publication_id)",
            "CREATE INDEX IF NOT EXISTS ix_publish_jobs_status_sched ON publish_jobs (status, scheduled_at)",
            "ALTER TABLE chat_publish_locks ADD COLUMN IF NOT EXISTS last_published_at TIMESTAMPTZ",
            "ALTER TABLE chat_publish_locks ADD COLUMN IF NOT EXISTS last_author_user_id BIGINT",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS payment_method_id VARCHAR(64)",
            "ALTER TABLE payments ADD COLUMN IF NOT EXISTS save_payment_method BOOLEAN NOT NULL DEFAULT false",
            # Напоминание об окончании подписки за 24 часа и уведомление об окончании
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMPTZ",
            "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS expired_notified_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_status_expires ON subscriptions (status, expires_at)",
            # Срок действия записи белого списка и индекс под выручку за период
            "ALTER TABLE whitelist_entries ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_payments_status_paid ON payments (status, paid_at)",
            # Оплата франшизы теперь идёт до создания бота (ТЗ «Оплата -> Создание
            # бота»): имя бота и api_key ещё не известны в момент оплаты.
            "ALTER TABLE whitelabel_partners ALTER COLUMN bot_username DROP NOT NULL",
            "ALTER TABLE whitelabel_partners ALTER COLUMN api_key DROP NOT NULL",
            # Партнёр сам вводит свои реквизиты ЮKassa через бота (ТЗ 6.6).
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS yookassa_shop_id VARCHAR(64)",
            "ALTER TABLE whitelabel_partners ADD COLUMN IF NOT EXISTS yookassa_secret_key TEXT",
        ):
            await conn.execute(text(stmt))

        # Tenant isolation: legacy rows remain in the primary bot's tenant (0).
        tenant_tables = (
            "users", "saved_contacts", "cities", "chats", "classification_logs", "subscriptions",
            "subscription_chats", "package_orders", "payments", "publications",
            "publish_jobs", "subscription_publication_logs", "whitelist_entries",
            "b2b_integrations", "chat_publish_locks", "oneshot_events",
            "publish_events", "admin_access", "app_settings",
            "tariff_categories", "admin_audit_log",
        )
        for table in tenant_tables:
            await conn.execute(text(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS partner_id INTEGER NOT NULL DEFAULT 0"
            ))
            await conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN partner_id SET DEFAULT 0"
            ))
            await conn.execute(text(
                f"ALTER TABLE {table} ALTER COLUMN partner_id SET NOT NULL"
            ))
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS ix_{table}_partner_id ON {table} (partner_id)"
            ))

        # Replace global uniqueness with tenant-local identity constraints.
        for stmt in (
            "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_telegram_id_key",
            "DROP INDEX IF EXISTS ix_users_telegram_id",
            "ALTER TABLE cities DROP CONSTRAINT IF EXISTS cities_key_key",
            "DROP INDEX IF EXISTS ix_cities_key",
            "ALTER TABLE whitelist_entries DROP CONSTRAINT IF EXISTS whitelist_entries_telegram_id_key",
            "DROP INDEX IF EXISTS ix_whitelist_entries_telegram_id",
            "ALTER TABLE admin_access DROP CONSTRAINT IF EXISTS admin_access_telegram_id_key",
            "DROP INDEX IF EXISTS ix_admin_access_telegram_id",
            "ALTER TABLE oneshot_events DROP CONSTRAINT IF EXISTS uq_oneshot_event",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_partner_telegram ON users (partner_id, telegram_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_cities_partner_key ON cities (partner_id, key)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_whitelist_partner_telegram ON whitelist_entries (partner_id, telegram_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_admin_access_partner_telegram ON admin_access (partner_id, telegram_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_oneshot_partner_event ON oneshot_events (partner_id, network, telegram_chat_id, message_id)",
        ):
            await conn.execute(text(stmt))

        # These dictionaries use a composite key so each clone can manage its own values.
        for table, columns in (("app_settings", "partner_id, key"), ("tariff_categories", "partner_id, code")):
            await conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {table}_pkey"))
            await conn.execute(text(f"ALTER TABLE {table} ADD CONSTRAINT {table}_pkey PRIMARY KEY ({columns})"))


# Версия справочника тарифов. Поднимать, когда названия или порядок категорий
# в `app/ml/categories.py` меняются по решению заказчика: на следующем старте
# сид один раз перепишет их в БД. Версия 1 — приведение к справочнику ТЗ 6.6.
TARIFF_CATALOG_VERSION = 1

# Версия справочника городов и групп. Поднимать, когда заказчик присылает новый
# перечень: на следующем старте сид один раз подтянет `CHAT_SEED` в БД целиком —
# с переименованиями, сменой города и сменой telegram id, — а группы, которых в
# перечне больше нет, отключит. Версия 1 — перечень от 14.09.2026, версия 2 —
# сверка username обеих групп НН с Telegram 20.09.2026.
CHAT_CATALOG_VERSION = 2


async def _seed_tariffs() -> None:
    """Наполнение и сверка справочника тарифов (ТЗ этапа 2, 6.6).

    Существующие строки при обычном старте не трогаем: цены и названия правит
    администратор из панели, и сид не должен затирать его правки.

    Исключение — разовая сверка по версии справочника. Названия и порядок
    категорий заданы в ТЗ, а в БД они попали ещё из этапа 1, поэтому одной
    правкой `CATEGORIES` их не догнать: строки давно созданы. Поднятая версия
    разрешает сиду один раз перезаписать `label` и `sort_order`, после чего
    отметка в `app_settings` закрывает эту дверь до следующего поднятия — и
    все дальнейшие переименования из панели живут как обычно.

    Цены сверка не трогает: они и так совпадают с ТЗ до рубля, а вот их
    администратор меняет штатно.
    """
    from sqlalchemy import select

    from app.ml.categories import CATEGORIES
    from app.models.entities import AppSetting, TariffCategory
    from app.services import settings_store
    from app.services.tariffs import INACTIVE_BY_DEFAULT

    async with SessionLocal() as session:
        rows = {
            row.code: row
            for row in (await session.scalars(select(TariffCategory))).all()
        }
        # Версию читаем прямо из таблицы, а не через settings_store: у него
        # кэш с TTL, который переживает очистку базы между тестами.
        marker = await session.scalar(
            select(AppSetting).where(AppSetting.key == settings_store.TARIFF_CATALOG_VERSION),
        )
        try:
            known_version = int(marker.value) if marker else 0
        except ValueError:
            known_version = 0
        reconcile = known_version < TARIFF_CATALOG_VERSION

        for order, cat in enumerate(CATEGORIES.values()):
            row = rows.get(cat.code)
            if row is None:
                session.add(
                    TariffCategory(
                        code=cat.code,
                        label=cat.label,
                        price_per_chat=cat.price_per_chat,
                        sort_order=order,
                        active=cat.code not in INACTIVE_BY_DEFAULT,
                        blocked=cat.blocked,
                        needs_review=cat.needs_review,
                    ),
                )
            elif reconcile:
                row.label = cat.label
                row.sort_order = order

        if reconcile:
            if marker is None:
                marker = AppSetting(key=settings_store.TARIFF_CATALOG_VERSION)
                session.add(marker)
            marker.value = str(TARIFF_CATALOG_VERSION)

        await session.commit()

    if reconcile:
        await settings_store.refresh()


async def _seed_places() -> None:
    """Наполнение и сверка справочника городов и групп (ТЗ этапа 2, 6.5).

    При обычном старте существующие строки не трогаем: города и чаты правит
    администратор из панели, и сид не должен затирать его правки.

    Исключение — разовая сверка по `CHAT_CATALOG_VERSION`. Раньше сид искал чат
    только по username и обновлял у найденного одно поле `network`: сменить
    telegram id, название или город через сид было нельзя. А заказчик присылает
    перечень целиком, и группы в нём успевают переименоваться и переехать.
    Поэтому на поднятой версии строку ищем сначала по telegram id (он переживает
    переименование группы), затем по username и только потом по прежнему
    username из шестого поля — и переписываем найденную целиком. Группы, которых
    в перечне нет, отключаем: удалять нельзя, на них ссылаются подписки и журнал
    публикаций.
    """
    from sqlalchemy import select

    from app.ml.categories import CITY_CHOICES
    from app.models.entities import AppSetting, Chat, City
    from app.services import settings_store
    from app.services.chat_network import infer_chat_network

    async with SessionLocal() as session:
        # Версию читаем прямо из таблицы, а не через settings_store: у него кэш
        # с TTL, который переживает очистку базы между тестами.
        marker = await session.scalar(
            select(AppSetting).where(AppSetting.key == settings_store.CHAT_CATALOG_VERSION),
        )
        try:
            known_version = int(marker.value) if marker else 0
        except ValueError:
            known_version = 0
        reconcile = known_version < CHAT_CATALOG_VERSION

        for key, label in CITY_CHOICES:
            exists = await session.scalar(select(City).where(City.key == key))
            if not exists:
                session.add(City(key=key, label=label))
        await session.commit()

        cities = {c.key: c for c in (await session.scalars(select(City))).all()}
        chats = list((await session.scalars(select(Chat))).all())
        by_chat_id = {c.telegram_chat_id: c for c in chats if c.telegram_chat_id}
        by_username = {(c.telegram_username or "").lower(): c for c in chats}
        seeded: set[int] = set()

        for idx, (city_key, topic, username, chat_id, title, legacy) in enumerate(CHAT_SEED):
            city = cities.get(city_key)
            if not city:
                continue
            network = infer_chat_network(username)
            row = (
                by_chat_id.get(chat_id)
                or by_username.get(username.lower())
                or (by_username.get(legacy.lower()) if legacy else None)
            )
            if row is None:
                row = Chat(
                    city_id=city.id,
                    topic=topic,
                    telegram_username=username,
                    telegram_chat_id=chat_id,
                    title=title,
                    network=network,
                    sort_order=idx,
                )
                session.add(row)
                await session.flush()
            elif reconcile:
                row.city_id = city.id
                row.topic = topic
                row.telegram_username = username
                row.telegram_chat_id = chat_id
                row.title = title
                row.sort_order = idx
                row.active = True
            row.network = network
            seeded.add(row.id)

        if reconcile:
            for chat in chats:
                if chat.id not in seeded and chat.active:
                    chat.active = False
            if marker is None:
                marker = AppSetting(key=settings_store.CHAT_CATALOG_VERSION)
                session.add(marker)
            marker.value = str(CHAT_CATALOG_VERSION)
        else:
            # Сеть у чатов вне перечня (заведены из панели) досчитываем всегда.
            for chat in chats:
                if chat.id in seeded:
                    continue
                expected = infer_chat_network(chat.telegram_username)
                if chat.network != expected:
                    chat.network = expected

        await session.commit()

    if reconcile:
        await settings_store.refresh()


async def seed() -> None:
    await wait_db()
    from pathlib import Path

    from app.ml.train import train_and_save

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await ensure_schema()

    await _seed_tariffs()
    await _seed_places()

    settings = get_settings()
    model_path = Path(settings.ml_model_path)
    data_path = Path(settings.training_data_path)
    if data_path.exists() and not model_path.exists():
        logger.info("Training ML model (first run)...")
        try:
            metrics = await train_and_save(data_path, model_path)
            logger.info("Model trained: accuracy=%.3f", metrics.get("accuracy", 0))
        except Exception:
            logger.exception("Model training failed; bot will start without ML")

    await _seed_hourly_ml_samples()
    await _maybe_retrain_ml()


async def _seed_hourly_ml_samples() -> None:
    from sqlalchemy import select

    from app.data.shabashka_hourly_samples import SHABASHKA_HOURLY_SAMPLES
    from app.models.entities import TrainingSample

    async with SessionLocal() as session:
        added = 0
        for category, text in SHABASHKA_HOURLY_SAMPLES:
            exists = await session.scalar(
                select(TrainingSample).where(
                    TrainingSample.target_category == category,
                    TrainingSample.text == text,
                ),
            )
            if exists:
                continue
            session.add(TrainingSample(text=text, target_category=category, source="hourly_rules"))
            added += 1
        if added:
            await session.commit()
            logger.info("Added %s hourly ML samples", added)


async def _maybe_retrain_ml() -> None:
    from pathlib import Path

    from app.ml.train import train_and_save

    settings = get_settings()
    model_path = Path(settings.ml_model_path)
    data_path = Path(settings.training_data_path)
    if not data_path.exists():
        return
    try:
        metrics = await train_and_save(data_path, model_path)
        from app.ml.classifier import get_classifier

        get_classifier().reload()
        logger.info("ML retrained after hourly samples: acc=%.3f", metrics.get("accuracy", 0))
    except Exception:
        logger.exception("ML retrain skipped")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(seed())
