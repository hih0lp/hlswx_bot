"""Сводка по подписке для экранов макета: город, чаты, объём публикаций."""

from __future__ import annotations

from sqlalchemy import select

from app.models.entities import Chat, City, Subscription, SubscriptionChat
from app.services.pricing import POSTS_VOLUME_LABELS, POSTS_VOLUME_LOW
from app.services.textfmt import chats_label


async def subscription_chats(session, subscription_id: int) -> tuple[str, list[Chat]]:
    """(название города, чаты подписки)."""
    links = (
        await session.scalars(
            select(SubscriptionChat).where(SubscriptionChat.subscription_id == subscription_id),
        )
    ).all()
    chats: list[Chat] = []
    city_label = "—"
    for link in links:
        chat = await session.scalar(select(Chat).where(Chat.id == link.chat_id))
        if not chat:
            continue
        chats.append(chat)
        if city_label == "—":
            city = await session.scalar(select(City).where(City.id == chat.city_id))
            if city:
                city_label = city.label
    return city_label, chats


async def subscription_summary(session, sub: Subscription) -> dict[str, str]:
    """Поля для шаблонов «Оформление подписки» / «Ваш тариф» / «Подписка активирована»."""
    city_label, chats = await subscription_chats(session, sub.id)
    return {
        "city": city_label,
        "chats": chats_label(len(chats)),
        "volume": POSTS_VOLUME_LABELS.get(sub.posts_volume or POSTS_VOLUME_LOW, POSTS_VOLUME_LABELS[POSTS_VOLUME_LOW]),
    }
