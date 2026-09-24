from __future__ import annotations

import math
import uuid
from decimal import Decimal

from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.entities import Payment, PaymentStatus


def rub_to_stars(amount_rub: int) -> int:
    rate = max(get_settings().stars_rub_rate, 0.01)
    return max(1, math.ceil(amount_rub / rate))


async def send_stars_topup_invoice(
    bot: Bot,
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    amount_rub: int,
) -> Payment:
    stars = rub_to_stars(amount_rub)
    invoice_id = f"hwls-stars-{user_id}-{uuid.uuid4().hex[:8]}"
    payment = Payment(
        user_id=user_id,
        amount=Decimal(amount_rub),
        purpose="topup",
        purpose_id=user_id,
        invoice_id=invoice_id,
        status=PaymentStatus.pending,
        provider="telegram_stars",
    )
    session.add(payment)
    await session.flush()

    await bot.send_invoice(
        chat_id=chat_id,
        title="Пополнение баланса HWLS Pay",
        description=f"Зачисление {amount_rub:,} ₽ на баланс",
        payload=f"topup_stars:{payment.id}:{amount_rub}",
        currency="XTR",
        prices=[LabeledPrice(label="Telegram Stars", amount=stars)],
        provider_token="",
    )
    await session.commit()
    await session.refresh(payment)
    return payment
