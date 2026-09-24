"""Журнал действий администратора (ТЗ этапа 2, раздел 7).

«Действия администратора или менеджера … фиксируются для последующей
диагностики». Проверяем саму запись и то, что сбой журнала не роняет
действие, ради которого он ведётся.
"""

from __future__ import annotations

import pytest

from app.models.entities import AdminRole
from app.services import access, admin_audit

ADMIN = 555


@pytest.fixture(autouse=True)
async def _admin(monkeypatch):
    monkeypatch.setattr(access, "_owner_ids", lambda: {ADMIN})
    await access.refresh()
    yield
    await access.refresh()


async def test_action_is_recorded_with_actor_and_role():
    await admin_audit.log_action(
        ADMIN, admin_audit.TARIFF_PRICE, target="RENT", detail="1000 → 1100",
    )

    rows = await admin_audit.recent()
    assert len(rows) == 1
    assert rows[0].actor_telegram_id == ADMIN
    assert rows[0].actor_role == AdminRole.admin.value
    assert rows[0].action == admin_audit.TARIFF_PRICE
    assert rows[0].target == "RENT"
    assert rows[0].detail == "1000 → 1100"


async def test_manager_role_is_recorded():
    manager = 666
    await access.grant(manager, AdminRole.manager, by_telegram_id=ADMIN)
    await admin_audit.log_action(manager, admin_audit.WHITELIST_ADD, target="@user")

    assert (await admin_audit.recent())[0].actor_role == AdminRole.manager.value


async def test_recent_is_newest_first_and_filterable():
    await admin_audit.log_action(ADMIN, admin_audit.WHITELIST_ADD, target="@a")
    await admin_audit.log_action(ADMIN, admin_audit.TARIFF_PRICE, target="RENT")
    await admin_audit.log_action(ADMIN, admin_audit.WHITELIST_ADD, target="@b")

    assert [r.target for r in await admin_audit.recent()] == ["@b", "RENT", "@a"]
    assert [r.target for r in await admin_audit.recent(action=admin_audit.WHITELIST_ADD)] == ["@b", "@a"]


async def test_long_target_is_truncated():
    await admin_audit.log_action(ADMIN, admin_audit.WHITELIST_ADD, target="x" * 500)
    assert len((await admin_audit.recent())[0].target) == 128


async def test_empty_target_becomes_null():
    await admin_audit.log_action(ADMIN, admin_audit.BROADCAST, target="")
    assert (await admin_audit.recent())[0].target is None


async def test_logging_failure_does_not_raise(monkeypatch):
    """Упавший журнал не должен отменять само действие администратора."""
    def boom(*args, **kwargs):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(admin_audit, "SessionLocal", boom)
    await admin_audit.log_action(ADMIN, admin_audit.TARIFF_PRICE, target="RENT")
