"""Напоминание об окончании подписки за 24 часа и уведомление о завершении.

Задача заказчика от 11.09.2026 плюс фрейм макета «⚠️ Подписка закончилась»
(`144:211`), который до сих пор было некому отправить.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import Subscription, SubscriptionStatus
from app.services.subscription_reminders import process_subscription_reminders
from tests.conftest import now_utc


@pytest.fixture(autouse=True)
def _bot(bot, monkeypatch):
    """Фоновый сервис берёт бота через get_bot() — подменяем на фейковый."""
    monkeypatch.setattr("app.bot.runtime.get_bot", lambda: bot, raising=True)
    return bot


async def _set_expiry(sub_id: int, delta: timedelta) -> None:
    async with SessionLocal() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == sub_id))
        sub.expires_at = now_utc() + delta
        await session.commit()


async def _reload(sub_id: int) -> Subscription:
    async with SessionLocal() as session:
        return await session.scalar(select(Subscription).where(Subscription.id == sub_id))


async def test_reminder_sent_once_within_24h(_bot, world):
    """Подписка кончается через 23 часа — уходит ровно одно напоминание."""
    await _set_expiry(world.sub_ids["sub1"], timedelta(hours=23))

    first = await process_subscription_reminders()
    assert first["reminded"] == 1

    notes = _bot.user_notifications(world.user_tg_ids["sub1"])
    assert len(notes) == 1
    assert "заканчивается завтра" in notes[0]

    # Повторный прогон молчит: отметка reminded_at уже стоит.
    second = await process_subscription_reminders()
    assert second["reminded"] == 0
    assert len(_bot.user_notifications(world.user_tg_ids["sub1"])) == 1
    assert (await _reload(world.sub_ids["sub1"])).reminded_at is not None


async def test_reminder_not_sent_too_early(_bot, world):
    """До конца двое суток — рано, не трогаем."""
    await _set_expiry(world.sub_ids["sub1"], timedelta(hours=48))

    assert (await process_subscription_reminders())["reminded"] == 0
    assert _bot.user_notifications(world.user_tg_ids["sub1"]) == []


async def test_expired_subscription_notified_and_closed(_bot, world):
    """Срок вышел: статус становится expired и уходит экран из макета."""
    await _set_expiry(world.sub_ids["sub1"], timedelta(minutes=-1))

    result = await process_subscription_reminders()
    assert result["expired"] == 1

    notes = _bot.user_notifications(world.user_tg_ids["sub1"])
    assert len(notes) == 1
    assert "Подписка закончилась" in notes[0]

    sub = await _reload(world.sub_ids["sub1"])
    assert sub.status is SubscriptionStatus.expired
    assert sub.expired_notified_at is not None

    # Второй прогон уже не находит активных подписок с истёкшим сроком.
    assert (await process_subscription_reminders())["expired"] == 0
    assert len(_bot.user_notifications(world.user_tg_ids["sub1"])) == 1


async def test_reminder_and_expiry_do_not_double_up(_bot, world):
    """Напоминание и уведомление об окончании — два разных сообщения подряд."""
    sub_id = world.sub_ids["sub1"]
    await _set_expiry(sub_id, timedelta(hours=1))
    assert (await process_subscription_reminders())["reminded"] == 1

    await _set_expiry(sub_id, timedelta(minutes=-1))
    assert (await process_subscription_reminders())["expired"] == 1

    notes = _bot.user_notifications(world.user_tg_ids["sub1"])
    assert len(notes) == 2
    assert "заканчивается завтра" in notes[0]
    assert "Подписка закончилась" in notes[1]


async def test_pending_payment_subscription_is_ignored(_bot, world):
    """Неоплаченная подписка в напоминания не попадает."""
    sub_id = world.sub_ids["sub2"]
    async with SessionLocal() as session:
        sub = await session.scalar(select(Subscription).where(Subscription.id == sub_id))
        sub.status = SubscriptionStatus.pending_payment
        sub.expires_at = now_utc() + timedelta(hours=2)
        await session.commit()

    result = await process_subscription_reminders()
    assert result == {"reminded": 0, "expired": 0}
    assert _bot.user_notifications(world.user_tg_ids["sub2"]) == []
