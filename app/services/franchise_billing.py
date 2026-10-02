"""Monthly plan billing for partner bots, charged through the platform shop."""
from __future__ import annotations

import logging
import uuid
from calendar import monthrange
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from yookassa.domain.exceptions.forbidden_error import ForbiddenError

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.entities import Payment, PaymentStatus, User, WhitelabelPartner
from app.services.tenant import partner_scope
from app.services.yookassa import YooKassaService

logger = logging.getLogger("franchise_billing")
TIER_PRICES = {"basic": 1500, "standard": 2500, "premium": 3500}
TIER_LABELS = {"basic": "Базовый", "standard": "Стандарт", "premium": "Премиум"}


class FranchiseBotAlreadyRegistered(ValueError):
    """Raised before checkout when a bot username is already claimed."""



def _next_month(value: datetime) -> datetime:
    month = value.month + 1
    year = value.year + (month > 12)
    month = (month - 1) % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


async def start_plan_checkout(partner_id: int, telegram_id: int, tier: str) -> str:
    """Create first card payment; YooKassa asks the owner to consent to saving it."""
    tier = (tier or "").lower()
    if tier not in TIER_PRICES:
        raise ValueError("Неизвестный тариф франшизы")
    with partner_scope(0):
        async with SessionLocal() as session:
            partner = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
            if not partner or partner.owner_telegram_id != telegram_id:
                raise PermissionError("Владелец бота не найден")
            # Recheck immediately before creating a payment. Old pending rows can
            # outlive the original onboarding attempt, so never trust that their
            # bot username is still available when the owner resumes checkout.
            # Payment now happens before the bot token is known (ТЗ «Оплата ->
            # Создание бота»), so there is nothing to recheck yet in that case.
            if partner.bot_username:
                duplicate_id = await session.scalar(
                    select(WhitelabelPartner.id).where(
                        func.lower(WhitelabelPartner.bot_username)
                        == partner.bot_username.lower(),
                        WhitelabelPartner.id != partner.id,
                    ).limit(1)
                )
                if duplicate_id is not None:
                    raise FranchiseBotAlreadyRegistered(
                        "Этот бот уже зарегистрирован. Оплата не создана."
                    )
            owner = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if owner is None:
                owner = User(telegram_id=telegram_id, username=partner.owner_username or "", full_name=partner.brand_title or partner.bot_username)
                session.add(owner)
                await session.flush()
            invoice = f"hwls-franchise-{partner.id}-{tier}-{uuid.uuid4().hex[:10]}"
            payment = Payment(user_id=owner.id, amount=Decimal(TIER_PRICES[tier]), purpose="franchise", purpose_id=partner.id, invoice_id=invoice, status=PaymentStatus.pending, save_payment_method=True)
            session.add(payment)
            await session.flush()
            yookassa = YooKassaService(get_settings())
            description = f"Тариф франшизы {TIER_LABELS[tier]} на месяц"
            metadata = {"purpose": "franchise", "partner_id": str(partner.id), "tier": tier, "invoice_id": invoice}
            amount = payment.amount
            try:
                try:
                    data = await yookassa.create_payment(
                        amount, description, metadata,
                        idempotence_key=invoice, save_payment_method=True,
                    )
                except ForbiddenError:
                    # Магазин платформы не подключён к автоплатежам («This store
                    # can't make recurring payments»). Без сохранения карты
                    # владелец всё равно может оплатить месяц: партнёр уйдёт в
                    # режим `manual`, а продлевать тариф придётся вручную.
                    logger.warning(
                        "Магазин ЮKassa не принимает автоплатежи — платёж франшизы partner_id=%s "
                        "создаётся без сохранения карты", partner_id,
                    )
                    payment.save_payment_method = False
                    data = await yookassa.create_payment(
                        amount, description, metadata,
                        idempotence_key=f"{invoice}-once", save_payment_method=False,
                    )
            except Exception:
                await session.rollback()
                # `partner` после rollback «протух»: обращение к его полям
                # даёт MissingGreenlet и прячет настоящую ошибку.
                logger.exception("Could not create franchise checkout partner_id=%s", partner_id)
                raise RuntimeError("Не удалось создать оплату франшизы") from None
            payment.provider_payment_id = data.get("id")
            payment.confirmation_url = YooKassaService.extract_confirmation_url(data)
            await session.commit()
            if not payment.confirmation_url:
                raise RuntimeError("ЮKassa не вернула ссылку для оплаты")
            return payment.confirmation_url


async def _notify(telegram_id: int, text: str) -> None:
    try:
        from app.bot.runtime import _get_main_bot
        await _get_main_bot().send_message(telegram_id, text)
    except Exception:
        logger.exception("Could not notify franchise owner id=%s", telegram_id)


async def prompt_for_bot_token(partner: WhitelabelPartner) -> None:
    """Payment succeeded but the clone bot doesn't exist yet (ТЗ «Оплата ->
    Создание бота»): put the owner's chat into the token-entry step so their
    very next message in the main bot is caught as the @BotFather token.
    """
    if not partner.owner_telegram_id:
        return
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey

    from app.bot.runtime import _get_main_bot, get_dispatcher
    from app.core.texts import FRANCHISE_CREATE_BOT_TEXT
    from app.services import banners
    from app.states.flows import FranchiseOnboarding

    try:
        bot = _get_main_bot()
        key = StorageKey(bot_id=bot.id, chat_id=partner.owner_telegram_id, user_id=partner.owner_telegram_id)
        state = FSMContext(storage=get_dispatcher().storage, key=key)
        await state.set_state(FranchiseOnboarding.waiting_bot_token)
        await state.update_data(franchise_tier=partner.franchise_tier, franchise_partner_id=partner.id)
        await banners.delete_last_screen(bot, partner.owner_telegram_id)
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✖️ Отмена", callback_data="franchise:cancel_token")],
        ])
        await bot.send_message(partner.owner_telegram_id, FRANCHISE_CREATE_BOT_TEXT, reply_markup=cancel_kb)
    except Exception:
        logger.exception("Could not prompt for bot token partner_id=%s", partner.id)


async def process_franchise_billing(now: datetime | None = None) -> dict[str, int]:
    """Run due monthly charges, reminders, and the three-day grace-period stop."""
    now = now or datetime.now(UTC)
    stats = {"charged": 0, "reminded": 0, "suspended": 0, "failed": 0}
    with partner_scope(0):
        async with SessionLocal() as session:
            partners = (await session.scalars(select(WhitelabelPartner).where(WhitelabelPartner.owner_telegram_id.isnot(None)))).all()
            for partner in partners:
                if not partner.active:
                    continue
                if partner.franchise_billing_status == "grace" and partner.franchise_grace_until and partner.franchise_grace_until <= now:
                    # Keep the bot process available so its owner can renew from
                    # the franchise panel; middleware gates every other update.
                    partner.franchise_billing_status = "suspended"
                    stats["suspended"] += 1
                    await session.commit()
                    from app.bot.runtime import set_partner_billing_state
                    set_partner_billing_state(partner.id, "suspended", partner.franchise_tier)
                    await _notify(partner.owner_telegram_id, "Бот приостановлен: оплата франшизы не прошла в течение 3 дней. Выберите тариф в разделе «Франшиза», чтобы возобновить работу.")
                    continue
                if partner.franchise_billing_status in {"active", "manual", "grace"} and partner.franchise_next_charge_at:
                    due = partner.franchise_next_charge_at
                    if due <= now + timedelta(days=3) and (not partner.franchise_last_notice_at or partner.franchise_last_notice_at < due - timedelta(days=3)):
                        partner.franchise_last_notice_at = now
                        stats["reminded"] += 1
                        await session.commit()
                        price = TIER_PRICES.get(partner.franchise_tier, TIER_PRICES["basic"])
                        if partner.franchise_payment_method_id:
                            reminder = f"Через три дня будет списана оплата тарифа франшизы: {price} ₽/мес."
                        else:
                            reminder = (
                                f"Через три дня заканчивается оплаченный месяц франшизы. "
                                f"Продлите тариф ({price} ₽/мес) в разделе «Франшиза»."
                            )
                        await _notify(partner.owner_telegram_id, reminder)
                    if due > now:
                        continue
                    if not partner.franchise_payment_method_id:
                        partner.franchise_billing_status = "grace"
                        partner.franchise_grace_until = partner.franchise_grace_until or (now + timedelta(days=3))
                        stats["failed"] += 1
                        await session.commit()
                        from app.bot.runtime import set_partner_billing_state
                        set_partner_billing_state(partner.id, "grace", partner.franchise_tier)
                        await _notify(partner.owner_telegram_id, "Оплаченный месяц франшизы закончился, а способ оплаты для автосписания не сохранён. Продлите тариф в разделе «Франшиза» — даём 3 дня, после этого бот будет приостановлен.")
                        continue
                    tier = partner.franchise_tier if partner.franchise_tier in TIER_PRICES else "basic"
                    invoice = f"hwls-franchise-{partner.id}-{tier}-{uuid.uuid4().hex[:10]}"
                    payment = Payment(user_id=partner.linked_user_id or 0, amount=Decimal(TIER_PRICES[tier]), purpose="franchise", purpose_id=partner.id, invoice_id=invoice, status=PaymentStatus.pending, payment_method_id=partner.franchise_payment_method_id)
                    if not partner.linked_user_id:
                        owner = await session.scalar(select(User).where(User.telegram_id == partner.owner_telegram_id))
                        if not owner:
                            owner = User(telegram_id=partner.owner_telegram_id, username=partner.owner_username or "", full_name=partner.brand_title or partner.bot_username)
                            session.add(owner)
                            await session.flush()
                        payment.user_id = owner.id
                    session.add(payment)
                    await session.flush()
                    try:
                        data = await YooKassaService(get_settings()).create_payment(
                            payment.amount, f"Автопродление франшизы {TIER_LABELS[tier]}",
                            {"purpose": "franchise", "partner_id": str(partner.id), "tier": tier, "invoice_id": invoice},
                            idempotence_key=invoice, payment_method_id=partner.franchise_payment_method_id,
                        )
                        payment.provider_payment_id = data.get("id")
                        if YooKassaService.payment_status(data) == "succeeded":
                            payment.status = PaymentStatus.succeeded
                            payment.paid_at = now
                            partner.franchise_next_charge_at = _next_month(now)
                            partner.franchise_grace_until = None
                            partner.franchise_last_notice_at = None
                            stats["charged"] += 1
                            partner.franchise_billing_status = "active"
                            await session.commit()
                            from app.bot.runtime import set_partner_billing_state
                            set_partner_billing_state(partner.id, "active", tier)
                            await _notify(partner.owner_telegram_id, f"Автоплатёж франшизы прошёл: {TIER_LABELS[tier]}, {TIER_PRICES[tier]} ₽.")
                            continue
                        raise RuntimeError("ЮKassa не подтвердила списание")
                    except Exception:
                        payment.status = PaymentStatus.failed
                        partner.franchise_billing_status = "grace"
                        partner.franchise_grace_until = partner.franchise_grace_until or (now + timedelta(days=3))
                        stats["failed"] += 1
                        await session.commit()
                        from app.bot.runtime import set_partner_billing_state
                        set_partner_billing_state(partner.id, "grace", partner.franchise_tier)
                        logger.exception("Franchise charge failed partner_id=%s", partner.id)
                        await _notify(partner.owner_telegram_id, "Не удалось списать ежемесячную оплату франшизы. Если оплата не пройдёт в течение 3 дней, бот будет приостановлен.")
            await session.commit()
    return stats
