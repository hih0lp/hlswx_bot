"""Настройки, которые администратор меняет из панели (ТЗ этапа 2, 6.10 и 7).

Тихий режим (20 минут) и интервал между публикациями (2 минуты) в этапе 1
были константами в `.env`, а `get_settings()` обёрнут в `@lru_cache` —
значение фиксировалось на весь процесс. По ТЗ они становятся настраиваемыми
и должны влиять на очередь **без перезапуска бота**, поэтому живут в БД.

Значения из `.env` остаются дефолтами: строка в `app_settings` появляется
только после того, как настройку меняли руками. Читают их воркер очереди и
экраны панели, то есть десятки раз в минуту, — держим в памяти процесса
и сбрасываем кэш на записи. TTL нужен на случай правки в обход бота.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.entities import AppSetting
from app.services.tenant import current_partner_id

logger = logging.getLogger("settings_store")

CACHE_TTL_SEC = 30

# Ключи, которые заводит этап 2. Дефолт берётся из .env через `default_of`.
QUEUE_SILENCE_MINUTES = "queue.silence_minutes"
QUEUE_USER_PAUSE_SEC = "queue.user_pause_sec"
QUEUE_JOB_TTL_MINUTES = "queue.job_ttl_minutes"

# Версия справочника тарифов: по ней сид один раз выравнивает названия и
# порядок категорий под ТЗ 6.6 и больше к ним не возвращается. В `.env` её
# нет — отсчёт с нуля. Читать эту настройку надо в обход кэша, см. seed.
TARIFF_CATALOG_VERSION = "tariffs.catalog_version"

# То же самое для справочника городов и групп, см. seed.CHAT_CATALOG_VERSION.
CHAT_CATALOG_VERSION = "chats.catalog_version"

_DEFAULTS = {
    QUEUE_SILENCE_MINUTES: lambda: get_settings().queue_silence_minutes,
    QUEUE_USER_PAUSE_SEC: lambda: get_settings().queue_user_pause_sec,
    QUEUE_JOB_TTL_MINUTES: lambda: get_settings().queue_job_ttl_minutes,
    TARIFF_CATALOG_VERSION: lambda: 0,
    CHAT_CATALOG_VERSION: lambda: 0,
}

_cache: dict[tuple[int, str], str] = {}
_loaded_at: float = 0.0


def default_of(key: str) -> int:
    """Значение из `.env`, если в БД настройку ещё не трогали."""
    factory = _DEFAULTS.get(key)
    if factory is None:
        raise KeyError(f"неизвестная настройка {key!r}")
    return int(factory())


async def refresh() -> None:
    global _cache, _loaded_at
    async with SessionLocal() as session:
        rows = (await session.scalars(
            select(AppSetting).execution_options(skip_partner_scope=True),
        )).all()
    _cache = {(row.partner_id, row.key): row.value for row in rows}
    _loaded_at = time.monotonic()


async def _ensure_loaded() -> None:
    if not _loaded_at or time.monotonic() - _loaded_at > CACHE_TTL_SEC:
        try:
            await refresh()
        except Exception:
            logger.exception("Не удалось прочитать настройки, остаёмся на значениях из .env")


async def get_int(key: str) -> int:
    """Текущее значение настройки: из БД, иначе из `.env`."""
    await _ensure_loaded()
    raw = _cache.get((current_partner_id(), key))
    if raw is None:
        return default_of(key)
    try:
        return int(raw)
    except ValueError:
        logger.warning("Настройка %s содержит не число (%r) — берём значение из .env", key, raw)
        return default_of(key)


async def set_int(key: str, value: int, *, by_telegram_id: int | None = None) -> None:
    """Записать настройку. Кэш сбрасывается сразу — очередь подхватит на следующем тике."""
    if key not in _DEFAULTS:
        raise KeyError(f"неизвестная настройка {key!r}")
    async with SessionLocal() as session:
        row = await session.scalar(select(AppSetting).where(AppSetting.key == key))
        if row is None:
            row = AppSetting(key=key)
            session.add(row)
        row.value = str(int(value))
        row.updated_by_telegram_id = by_telegram_id
        await session.commit()
    await refresh()


async def queue_silence_minutes() -> int:
    """Тишина группы после публикации платного объявления (ТЗ 6.10)."""
    return await get_int(QUEUE_SILENCE_MINUTES)


async def queue_user_pause_sec() -> int:
    """Минимальный интервал между сообщениями разных пользователей в группе."""
    return await get_int(QUEUE_USER_PAUSE_SEC)


async def queue_job_ttl_minutes() -> int:
    """Сколько задача ждёт в очереди до отмены. В панель не вынесена."""
    return await get_int(QUEUE_JOB_TTL_MINUTES)
