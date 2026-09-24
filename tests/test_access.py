"""Роли доступа к админ-панели (ТЗ этапа 2, разделы 6.11 и 7).

Администратору доступны все разделы, менеджеру — все, кроме «Управления».
Отдельно проверяем аварийный вход: владелец из .env остаётся администратором
даже при пустой таблице доступов, иначе панель можно потерять целиком.
"""

from __future__ import annotations

import pytest

from app.models.entities import AdminRole
from app.services import access

OWNER = 111
MANAGER = 222
ADMIN = 333
STRANGER = 444


@pytest.fixture(autouse=True)
async def _owner(monkeypatch):
    """Подменяем список владельцев из .env и чистим кэш ролей между тестами."""
    monkeypatch.setattr(access, "_owner_ids", lambda: {OWNER})
    await access.refresh()
    yield
    await access.refresh()


async def test_owner_is_admin_without_any_grant():
    """Таблица доступов пуста, но владелец из .env всё равно администратор."""
    assert await access.get_role(OWNER) is AdminRole.admin
    assert access.is_admin(OWNER)
    assert access.is_staff(OWNER)


async def test_stranger_has_no_access():
    assert await access.get_role(STRANGER) is None
    assert not access.is_staff(STRANGER)
    assert not access.is_admin(STRANGER)


async def test_manager_is_staff_but_not_admin():
    """Менеджер попадает в панель, но не в «Управление» (ТЗ 6.11)."""
    await access.grant(MANAGER, AdminRole.manager, by_telegram_id=OWNER, username="manager")

    assert await access.get_role(MANAGER) is AdminRole.manager
    assert access.is_staff(MANAGER)
    assert not access.is_admin(MANAGER)


async def test_grant_admin_and_change_role():
    await access.grant(ADMIN, AdminRole.manager, by_telegram_id=OWNER)
    assert access.is_admin(ADMIN) is False

    # Повторная выдача меняет роль, а не плодит вторую запись.
    await access.grant(ADMIN, AdminRole.admin, by_telegram_id=OWNER)
    assert access.is_admin(ADMIN) is True
    assert len(await access.list_access()) == 1


async def test_revoke():
    await access.grant(MANAGER, AdminRole.manager, by_telegram_id=OWNER)
    assert access.is_staff(MANAGER)

    assert await access.revoke(MANAGER) is True
    assert not access.is_staff(MANAGER)
    # Повторный отзыв уже ничего не находит.
    assert await access.revoke(MANAGER) is False


async def test_owner_cannot_be_revoked():
    """Роль владельца не из таблицы, отзывать нечего — доступ остаётся."""
    assert await access.revoke(OWNER) is False
    assert access.is_admin(OWNER)


async def test_staff_ids_covers_owners_and_grants():
    """Служебные уведомления уходят всей смене, а не только владельцам."""
    await access.grant(MANAGER, AdminRole.manager, by_telegram_id=OWNER)
    assert await access.staff_ids() == {OWNER, MANAGER}


async def test_cache_is_reread_after_direct_db_change():
    """Правка в обход бота доезжает после refresh() — на это и рассчитан TTL."""
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.entities import AdminAccess

    await access.grant(MANAGER, AdminRole.manager, by_telegram_id=OWNER)
    async with SessionLocal() as session:
        row = await session.scalar(select(AdminAccess).where(AdminAccess.telegram_id == MANAGER))
        row.role = AdminRole.admin
        await session.commit()

    assert access.is_admin(MANAGER) is False  # кэш ещё старый
    await access.refresh()
    assert access.is_admin(MANAGER) is True


# --------------------------------------------------------- фильтры роутеров


async def test_filters_let_admin_through_and_stop_stranger():
    """IsStaff и IsAdmin — то, чем закрыты роутеры разделов."""
    from types import SimpleNamespace

    from app.bot.filters import IsAdmin, IsStaff

    event = SimpleNamespace(from_user=SimpleNamespace(id=OWNER))
    assert await IsStaff()(event) == {"role": AdminRole.admin}
    assert await IsAdmin()(event) == {"role": AdminRole.admin}

    stranger = SimpleNamespace(from_user=SimpleNamespace(id=STRANGER))
    assert await IsStaff()(stranger) is False
    assert await IsAdmin()(stranger) is False


async def test_manager_is_stopped_by_admin_only_filter():
    """Менеджер входит в панель, но «Управление» для него закрыто (ТЗ 6.11)."""
    from types import SimpleNamespace

    from app.bot.filters import IsAdmin, IsStaff

    await access.grant(MANAGER, AdminRole.manager, by_telegram_id=OWNER)
    event = SimpleNamespace(from_user=SimpleNamespace(id=MANAGER))

    assert await IsStaff()(event) == {"role": AdminRole.manager}
    assert await IsAdmin()(event) is False


async def test_manage_router_is_admin_only():
    """Фильтр действительно висит на роутерах раздела «Управление»."""
    from app.bot.filters import IsAdmin
    from app.handlers.admin import access_grant, manage, messages

    for module in (manage, access_grant, messages):
        for observer in (module.router.callback_query, module.router.message):
            assert any(
                isinstance(f.callback, IsAdmin) for f in observer._handler.filters
            ), module.__name__
