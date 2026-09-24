"""Замечания заказчика от 17.09.2026 (task6.md) — по одному тесту на жалобу.

1. «С уже купленной подпиской не получается выставить объявление» — сообщение
   с фото приходило пустым (`message.text` у картинки `None`), и пользователь
   упирался в экран «Не удалось определить категорию».
2. «Поставить возможность отправлять /фото/текст+фото» — фото без подписи
   принимаем через OCR.
3. «Человек из белого списка не может купить подписку в другом городе, только
   один город появляется» — запись белого списка резала каталог городов.
4. «Ошибки обработки пусть ежедневно обновляется, чтоб не висел мёртвым
   грузом» — счётчик считал провалы за всё время.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.entities import (
    City,
    Payment,
    PaymentStatus,
    PublishJob,
    WhitelistEntry,
)
from app.services.admin_stats import gather_dashboard_stats
from app.services.media import read_ad_content

# --------------------------------------------------------- объявление с фото


class _Photo:
    def __init__(self, file_id: str) -> None:
        self.file_id = file_id


def _message(*, text=None, caption=None, photo=None, ocr: str = ""):
    async def get_file(file_id):
        return SimpleNamespace(file_path=f"photos/{file_id}.jpg")

    async def download_file(path):
        return SimpleNamespace(read=lambda: b"bytes")

    return SimpleNamespace(
        text=text,
        caption=caption,
        photo=photo,
        bot=SimpleNamespace(get_file=get_file, download_file=download_file),
    )


@pytest.mark.asyncio
async def test_plain_text_ad_is_read_as_before():
    content = await read_ad_content(_message(text="  Нужны грузчики на склад  "))
    assert content.text == "Нужны грузчики на склад"
    assert content.photo_id is None
    assert not content.empty


@pytest.mark.asyncio
async def test_photo_with_caption_gives_text_and_picture():
    """Главная причина жалобы: подпись лежит в caption, а читали только text."""
    content = await read_ad_content(
        _message(caption="Требуются охранники, вахта 15/15", photo=[_Photo("small"), _Photo("big")]),
    )
    assert content.text == "Требуются охранники, вахта 15/15"
    assert content.photo_id == "big", "берём самый крупный вариант картинки"
    assert not content.empty


@pytest.mark.asyncio
async def test_photo_without_caption_goes_through_ocr(monkeypatch):
    monkeypatch.setattr(
        "app.ml.image_ocr.ocr_image_bytes", lambda data: "Грузчики, 3000 руб за смену",
    )
    content = await read_ad_content(_message(photo=[_Photo("big")]))
    assert content.text == "Грузчики, 3000 руб за смену"
    assert content.photo_id == "big"
    assert content.from_ocr is True


@pytest.mark.asyncio
async def test_unreadable_photo_stays_empty(monkeypatch):
    """OCR не справился — экран с просьбой добавить текст остаётся как был."""

    def boom(data):
        raise RuntimeError("tesseract не установлен")

    monkeypatch.setattr("app.ml.image_ocr.ocr_image_bytes", boom)
    content = await read_ad_content(_message(photo=[_Photo("big")]))
    assert content.empty
    assert content.photo_id == "big"


def test_fraud_check_is_off_by_default():
    """У кого подписка оплачена — объявление засчитывается автоматически."""
    from app.config import get_settings

    assert get_settings().fraud_check_enabled is False


def test_user_photo_goes_directly_not_through_relay():
    """`file_id` действителен только для нашего бота — релею его отдавать нельзя."""
    from app.services.publish import _photo_is_ours

    assert _photo_is_ours("AgACAgIAAxkBAAI") is True
    assert _photo_is_ours("https://example.com/ad.jpg") is False
    assert _photo_is_ours(None) is False


# ------------------------------------------- белый список и каталог городов


@pytest.mark.asyncio
async def test_whitelist_entry_does_not_shrink_the_city_catalog(world):
    """Запись на «msk» больше не прячет остальные города в мастере подписки."""
    from app.handlers.subscription import _available_cities

    async with SessionLocal() as session:
        session.add(City(key="kzn", label="Казань", active=True))
        session.add(
            WhitelistEntry(
                telegram_id=world.user_tg_ids["wl"],
                username="whitelisted",
                city_keys=json.dumps(["msk"]),
                chat_ids="[]",
            ),
        )
        await session.commit()

    async with SessionLocal() as session:
        cities = await _available_cities(session)

    keys = {city.key for city in cities}
    assert keys == {"msk", "kzn"}, f"в мастере остались только {keys}"


@pytest.mark.asyncio
async def test_paid_subscription_publishes_outside_the_whitelist_scope(world, enqueue):
    """Купленная подписка не сверяется с границами записи белого списка."""
    async with SessionLocal() as session:
        session.add(
            WhitelistEntry(
                telegram_id=world.user_tg_ids["sub1"],
                username="subscriber_one",
                city_keys=json.dumps(["kzn"]),
                chat_ids="[]",
            ),
        )
        await session.commit()

    assert await enqueue("sub1") == len(world.chat_ids)


@pytest.mark.asyncio
async def test_free_whitelist_access_still_respects_its_scope(world, enqueue):
    """А бесплатный доступ по-прежнему ограничен своими городами."""
    async with SessionLocal() as session:
        session.add(
            WhitelistEntry(
                telegram_id=world.user_tg_ids["wl"],
                username="whitelisted",
                city_keys=json.dumps(["kzn"]),
                chat_ids="[]",
            ),
        )
        await session.commit()

    assert await enqueue("wl") == 0, "подписка белого списка обязана держаться своих городов"


# ------------------------------------------------ счётчики «требует внимания»


@pytest.mark.asyncio
async def test_attention_block_counts_only_today(world):
    """Вчерашние провалы и платежи в сегодняшнюю сводку не попадают."""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=2)

    async with SessionLocal() as session:
        for failed_at, status in (
            (now, "failed"),
            (now, "expired"),
            (yesterday, "failed"),
            (yesterday, "expired"),
        ):
            session.add(
                PublishJob(
                    chat_id=world.chat_ids[0],
                    text="объявление",
                    contact="@who",
                    status=status,
                    failed_at=failed_at,
                    error="boom",
                ),
            )
        for created_at in (now, yesterday):
            session.add(
                Payment(
                    user_id=world.user_ids["sub1"],
                    invoice_id=f"sub-{created_at.timestamp()}",
                    amount=1500,
                    purpose="subscription",
                    purpose_id=world.sub_ids["sub1"],
                    status=PaymentStatus.pending,
                    created_at=created_at,
                ),
            )
        await session.commit()

    stats = await gather_dashboard_stats()
    assert stats["queue_failed"] == 2, "считаем и failed, и expired, но только сегодняшние"
    assert stats["pending_payments"] == 1


@pytest.mark.asyncio
async def test_stale_pending_payments_are_closed(world):
    """Платёж, зависший дольше двух суток, уходит в «отменён» и не копится."""
    from app.services.payment_reconcile import cancel_stale_payments

    async with SessionLocal() as session:
        session.add(
            Payment(
                user_id=world.user_ids["sub1"],
                invoice_id="sub-stale",
                amount=1500,
                purpose="subscription",
                purpose_id=world.sub_ids["sub1"],
                status=PaymentStatus.pending,
                created_at=datetime.now(UTC) - timedelta(days=3),
            ),
        )
        session.add(
            Payment(
                user_id=world.user_ids["sub1"],
                invoice_id="sub-fresh",
                amount=1500,
                purpose="subscription",
                purpose_id=world.sub_ids["sub1"],
                status=PaymentStatus.pending,
                created_at=datetime.now(UTC),
            ),
        )
        await session.commit()

    assert await cancel_stale_payments() == 1

    async with SessionLocal() as session:
        fresh = await session.scalar(select(Payment).where(Payment.invoice_id == "sub-fresh"))
        stale = await session.scalar(select(Payment).where(Payment.invoice_id == "sub-stale"))
    assert fresh.status == PaymentStatus.pending
    assert stale.status == PaymentStatus.canceled


def test_queue_minutes_are_declined():
    """«1 минут» в макете не значится — там «2 минуты»."""
    from app.core import admin_texts as T
    from app.services.textfmt import minutes_label

    assert minutes_label(1) == "1 минута"
    assert minutes_label(2) == "2 минуты"
    assert minutes_label(5) == "5 минут"
    assert minutes_label(20) == "20 минут"
    assert "минут</b>" not in T.QUEUE_INTERVAL, "слово подставляется вместе с числом"


def test_panel_shows_queue_as_a_metric():
    """Кадр 337:208: «В очереди» стоит в сводке, а не в блоке «Требует внимания»."""
    from app.core import admin_texts as T

    assert "{queued}" in T.PANEL
    assert "В очереди" not in T.PANEL_ATTENTION


def test_attention_header_is_set_off_by_a_blank_line():
    """Кадр 337:233: у «Админки» отступ между детьми 24 — это пустая строка.

    Внутри блоков отступ 16, то есть обычный перенос; заказчик заметил, что
    в боте после заголовка не хватает именно пустой строки (task6.md).
    """
    from app.core import admin_texts as T

    assert T.PANEL_ATTENTION.startswith("<b>⚠️ Требует внимания</b>\n\n")


def test_expired_whitelist_entry_is_not_marked_green():
    """В карточке значок шёл до статуса и у истёкшей записи выходило «🟢 Истёк»."""
    from app.core import admin_texts as T

    assert "🟢" not in T.WL_CARD
    assert T.WL_STATUS_ACTIVE.startswith("🟢")
    assert T.WL_STATUS_EXPIRED.startswith("🔴")
