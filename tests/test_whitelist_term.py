"""Срок действия записи белого списка (ТЗ этапа 2, 6.3).

В макете у записи есть срок — «♾ Навсегда» или «📅 До даты», и на экране
списка отдельно показано, сколько записей истекло. До этапа 2 поля срока не
было вовсе, поэтому важно, что истёкшая запись действительно перестаёт
давать права, а не только помечается в интерфейсе.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.db.session import SessionLocal
from app.models.entities import WhitelistEntry
from app.services import whitelist

TG_ID = 8001
USERNAME = "vip_user"


async def _entry(expires_at) -> WhitelistEntry:
    async with SessionLocal() as session:
        row = WhitelistEntry(
            telegram_id=TG_ID,
            username=USERNAME,
            city_keys="[]",
            chat_ids="[]",
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()
        return row


async def test_entry_without_term_is_forever():
    """Записи, заведённые до появления срока, продолжают действовать."""
    await _entry(None)
    async with SessionLocal() as session:
        assert await whitelist.get_whitelist_entry_for_user(session, TG_ID) is not None


async def test_future_term_is_active():
    await _entry(datetime.now(UTC) + timedelta(days=3))
    async with SessionLocal() as session:
        assert await whitelist.get_whitelist_entry_for_user(session, TG_ID) is not None


async def test_expired_entry_gives_no_rights():
    await _entry(datetime.now(UTC) - timedelta(minutes=1))
    async with SessionLocal() as session:
        assert await whitelist.get_whitelist_entry_for_user(session, TG_ID) is None
        assert await whitelist.get_whitelist_entry(session, TG_ID) is None


async def test_expired_entry_is_still_visible_to_admin():
    """В панели истёкшая запись должна показываться — её видно и можно удалить."""
    await _entry(datetime.now(UTC) - timedelta(minutes=1))
    async with SessionLocal() as session:
        found = await whitelist.get_whitelist_entry(session, TG_ID, include_expired=True)
        assert found is not None
        assert not whitelist.entry_is_active(found)


async def test_lookup_by_username_respects_term():
    await _entry(datetime.now(UTC) - timedelta(days=1))
    async with SessionLocal() as session:
        assert await whitelist.get_whitelist_entry_by_username(session, USERNAME) is None
        assert await whitelist.get_whitelist_entry_by_username(
            session, USERNAME, include_expired=True,
        ) is not None


@pytest.mark.parametrize("delta,expected", [(timedelta(seconds=5), True), (timedelta(seconds=-5), False)])
def test_entry_is_active_boundary(delta, expected):
    entry = WhitelistEntry(telegram_id=1, expires_at=datetime.now(UTC) + delta)
    assert whitelist.entry_is_active(entry) is expected
