"""Лимит публикаций в день по объёму подписки.

По решению заказчика от 14.09.2026 безлимитного варианта больше нет: третий
вариант — 30 публикаций в день, и он же достаётся записям белого списка.
"""

from __future__ import annotations

from app.db.session import SessionLocal
from app.services.pricing import (
    POSTS_VOLUME_LOW,
    POSTS_VOLUME_MID,
    POSTS_VOLUME_UNLIMITED,
)
from app.services.volume_limits import (
    UNLIMITED_DAILY_LIMIT,
    check_volume_allowed,
    daily_limit_for_volume,
    log_publication,
)


def test_limits_by_volume():
    assert daily_limit_for_volume(POSTS_VOLUME_LOW) == 3
    assert daily_limit_for_volume(POSTS_VOLUME_MID) == 10
    assert daily_limit_for_volume(POSTS_VOLUME_UNLIMITED) == UNLIMITED_DAILY_LIMIT == 30


def test_unknown_volume_falls_back_to_the_smallest():
    assert daily_limit_for_volume("whatever") == 3


async def test_thirty_first_publication_is_refused(world):
    """Тридцать публикаций проходят, тридцать первая — нет."""
    sub_id = world.sub_ids["sub1"]

    async with SessionLocal() as session:
        for _ in range(UNLIMITED_DAILY_LIMIT - 1):
            await log_publication(session, sub_id)
        await session.commit()

        allowed, message = await check_volume_allowed(session, sub_id, POSTS_VOLUME_UNLIMITED)
        assert allowed and message == ""

        await log_publication(session, sub_id)
        await session.commit()

        allowed, message = await check_volume_allowed(session, sub_id, POSTS_VOLUME_UNLIMITED)

    assert not allowed
    assert "30" in message
    assert "в день" in message


async def test_whitelist_subscription_is_capped_too(world):
    """У записи белого списка тот же ключ объёма — значит и тот же лимит."""
    async with SessionLocal() as session:
        for _ in range(UNLIMITED_DAILY_LIMIT):
            await log_publication(session, world.sub_ids["wl"])
        await session.commit()

        allowed, _ = await check_volume_allowed(
            session, world.sub_ids["wl"], POSTS_VOLUME_UNLIMITED,
        )

    assert not allowed
