"""Роли доступа к админ-панели (ТЗ этапа 2, разделы 6.11 и 7).

Две роли: «Администратор» видит все разделы, «Менеджер» — все, кроме
«Управления». До этапа 2 ролей не было вовсе: администратором считался любой
telegram_id из `ADMIN_IDS` / `OWNER_IDS`, а проверка `_is_admin` была
скопирована в пять файлов.

Владельцы из `.env` остаются администраторами всегда и в таблицу не
попадают — это аварийный вход, иначе панель можно потерять целиком, отозвав
у себя последнюю роль.

Ролей в системе единицы, меняются они редко, а читать их приходится из
синхронного кода (сборщики клавиатур), поэтому таблица целиком лежит в
памяти процесса. Кэш обновляется при каждой выдаче и отзыве, а на случай
правки в обход бота — ещё и по таймауту.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.entities import AdminAccess, AdminRole

logger = logging.getLogger("access")

# Роль, выданная в обход бота (правкой в БД), доедет не позже чем за минуту.
CACHE_TTL_SEC = 60

_cache: dict[int, AdminRole] = {}
_loaded_at: float = 0.0


def _owner_ids() -> set[int]:
    return get_settings().admin_ids_set


async def refresh() -> None:
    """Перечитать таблицу доступов в память."""
    global _cache, _loaded_at
    async with SessionLocal() as session:
        rows = (await session.scalars(select(AdminAccess))).all()
    _cache = {row.telegram_id: row.role for row in rows}
    _loaded_at = time.monotonic()


async def ensure_loaded() -> None:
    """Подтянуть кэш, если он пуст или устарел."""
    if not _loaded_at or time.monotonic() - _loaded_at > CACHE_TTL_SEC:
        try:
            await refresh()
        except Exception:
            logger.exception("Не удалось прочитать таблицу доступов")


def role_of(telegram_id: int | None) -> AdminRole | None:
    """Роль по кэшу. Синхронная — её зовут в том числе сборщики клавиатур.

    Пока кэш не прогрет, права есть только у владельцев из `.env`: это
    безопасный недобор прав, а не перебор.
    """
    if not telegram_id:
        return None
    if telegram_id in _owner_ids():
        return AdminRole.admin
    return _cache.get(telegram_id)


def is_staff(telegram_id: int | None) -> bool:
    """Есть доступ к панели — администратор или менеджер."""
    return role_of(telegram_id) is not None


def is_admin(telegram_id: int | None) -> bool:
    """Только администратор: разделы «Управления» (ТЗ 6.11)."""
    return role_of(telegram_id) is AdminRole.admin


async def get_role(telegram_id: int | None) -> AdminRole | None:
    """Роль со свежим кэшем — для обработчиков, где есть await."""
    await ensure_loaded()
    return role_of(telegram_id)


async def grant(
    telegram_id: int,
    role: AdminRole,
    *,
    by_telegram_id: int | None = None,
    username: str | None = None,
) -> None:
    """Выдать или сменить роль (экран «Управление → Доступы»)."""
    async with SessionLocal() as session:
        row = await session.scalar(select(AdminAccess).where(AdminAccess.telegram_id == telegram_id))
        if row is None:
            row = AdminAccess(telegram_id=telegram_id)
            session.add(row)
        row.role = role
        row.username = username or row.username
        row.granted_by_telegram_id = by_telegram_id
        await session.commit()
    await refresh()


async def revoke(telegram_id: int) -> bool:
    """Отозвать доступ. False — записи не было.

    Владельца из `.env` отозвать нельзя: его роль не из таблицы.
    """
    async with SessionLocal() as session:
        row = await session.scalar(select(AdminAccess).where(AdminAccess.telegram_id == telegram_id))
        if row is None:
            return False
        await session.delete(row)
        await session.commit()
    await refresh()
    return True


async def list_access() -> list[AdminAccess]:
    """Выданные доступы — для экрана списка."""
    async with SessionLocal() as session:
        return list((await session.scalars(select(AdminAccess).order_by(AdminAccess.id))).all())


async def staff_ids() -> set[int]:
    """Все, кому уходят служебные уведомления: владельцы плюс выданные роли."""
    await ensure_loaded()
    return _owner_ids() | set(_cache)


async def admin_ids() -> set[int]:
    """Только администраторы — для уведомлений со ссылкой в «Управление».

    Менеджеру такое уведомление слать бессмысленно: раздел закрыт фильтром
    `IsAdmin`, и нажатие кнопки у него просто ничего не сделает (ТЗ 6.11, 7).
    """
    await ensure_loaded()
    return _owner_ids() | {
        telegram_id for telegram_id, role in _cache.items() if role is AdminRole.admin
    }
