from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import SubscriptionPublicationLog
from app.services.pricing import (
    POSTS_VOLUME_LOW,
    POSTS_VOLUME_MID,
    POSTS_VOLUME_UNLIMITED,
)

# Потолок третьего варианта. До 14.09.2026 он был безлимитным, но заказчик
# попросил лимит «для всех» — в том числе для записей белого списка, которым
# `whitelist.provision_whitelist_subscription` выдаёт тот же ключ объёма.
# Сам ключ остался прежним: на него ссылаются уже оплаченные подписки.
UNLIMITED_DAILY_LIMIT = 30


def daily_limit_for_volume(volume: str) -> int | None:
    if volume == POSTS_VOLUME_LOW:
        return 3
    if volume == POSTS_VOLUME_MID:
        return 10
    if volume == POSTS_VOLUME_UNLIMITED:
        return UNLIMITED_DAILY_LIMIT
    return 3


def _day_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def count_publications_today(session: AsyncSession, subscription_id: int) -> int:
    start = _day_start()
    end = start + timedelta(days=1)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SubscriptionPublicationLog)
            .where(
                SubscriptionPublicationLog.subscription_id == subscription_id,
                SubscriptionPublicationLog.created_at >= start,
                SubscriptionPublicationLog.created_at < end,
            ),
        )
        or 0,
    )


async def check_volume_allowed(session: AsyncSession, subscription_id: int, posts_volume: str) -> tuple[bool, str]:
    limit = daily_limit_for_volume(posts_volume)
    # Безлимитных вариантов сейчас нет, но ветка страхует неизвестное значение.
    if limit is None:
        return True, ""
    used = await count_publications_today(session, subscription_id)
    if used >= limit:
        return False, (
            f"Лимит тарифа: <b>{limit}</b> публикаций в день.\n"
            f"Сегодня уже отправлено: <b>{used}</b>.\n\n"
            "Дождитесь завтра или оформите подписку с большим объёмом."
        )
    return True, ""


async def log_publication(session: AsyncSession, subscription_id: int) -> None:
    session.add(SubscriptionPublicationLog(subscription_id=subscription_id))
    await session.flush()
