from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.entities import Payment, PaymentStatus, WhitelabelPartner
from app.services.notifications import notify_payment_success
from app.services.payments import mark_payment_succeeded, payment_provider_for_partner
from app.services.tenant import partner_scope
from app.services.yookassa import YooKassaService

logger = logging.getLogger("payment_reconcile")

# Дольше этого срока счёт в ЮKassa не живёт: если за двое суток он не пришёл в
# «succeeded», ждать больше нечего. Тот же горизонт, что у сверки ниже.
STALE_AFTER = timedelta(hours=48)


async def cancel_stale_payments() -> int:
    """Закрыть платежи, которые навсегда зависли в «в обработке».

    Без этого `pending` копились в базе бесконечно: сверка умела переводить их
    только в «оплачено», а отменять — никто. На главном экране панели они висели
    строкой «⏳ Платежи в обработке» и не убывали (правка 17.09.2026).
    """
    cutoff = datetime.now(UTC) - STALE_AFTER
    async with SessionLocal() as session:
        stale = (
            await session.scalars(
                select(Payment).where(
                    Payment.status == PaymentStatus.pending,
                    Payment.created_at < cutoff,
                ),
            )
        ).all()
        for payment in stale:
            payment.status = PaymentStatus.canceled
        if stale:
            await session.commit()
            logger.info("Отменено зависших платежей: %s", len(stale))
    return len(stale)


async def reconcile_pending_payments() -> int:
    """Резервная проверка оплат, если webhook ЮKassa не сработал."""
    settings = get_settings()
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        return 0

    since = datetime.now(UTC) - STALE_AFTER
    processed = 0
    async with SessionLocal() as session:
        payments = (
            await session.scalars(
                select(Payment)
                .where(
                    Payment.status == PaymentStatus.pending,
                    Payment.provider_payment_id.isnot(None),
                    Payment.created_at >= since,
                )
                .order_by(Payment.id.desc())
                .limit(100)
                .execution_options(skip_partner_scope=True),
            )
        ).all()

        for payment in payments:
            try:
                yk = await payment_provider_for_partner(settings, payment.partner_id)
                remote = await yk.get_payment(payment.provider_payment_id)
            except Exception:
                logger.exception("Reconcile check failed for payment %s", payment.id)
                continue
            if YooKassaService.payment_status(remote) != "succeeded":
                continue
            if payment.status == PaymentStatus.succeeded:
                continue
            with partner_scope(payment.partner_id):
                scoped_payment = await session.scalar(select(Payment).where(Payment.id == payment.id))
                if not scoped_payment or scoped_payment.status != PaymentStatus.pending:
                    continue
                result = await mark_payment_succeeded(session, scoped_payment)
            processed += 1
            purpose_id = payment.purpose_id
            user_id = payment.user_id
            topup_amount = payment.amount if payment.purpose == "topup" else None
            logger.info("Reconciled payment %s -> %s", payment.id, result)
            if result == "franchise_activated":
                partner = await session.scalar(
                    select(WhitelabelPartner)
                    .where(WhitelabelPartner.id == purpose_id)
                    .execution_options(skip_partner_scope=True)
                )
                if partner:
                    from app.bot.runtime import request_partner_bots_reload, set_partner_billing_state

                    set_partner_billing_state(
                        partner.id,
                        partner.franchise_billing_status,
                        partner.franchise_tier,
                    )
                    # Reconciliation is the fallback when the test webhook URL
                    # is unreachable; activation must still start the new clone.
                    request_partner_bots_reload()
                    if partner.owner_telegram_id:
                        from app.bot.runtime import _get_main_bot

                        note = (
                            "Оплата франшизы прошла. Автопродление подключено."
                            if partner.franchise_payment_method_id
                            else "Оплата франшизы прошла. Автопродление не включилось: ЮKassa не сохранила способ оплаты."
                        )
                        try:
                            await _get_main_bot().send_message(partner.owner_telegram_id, note)
                        except Exception:
                            logger.exception(
                                "Could not notify franchise owner after reconciliation partner_id=%s",
                                partner.id,
                            )
            elif result == "franchise_awaiting_token":
                partner = await session.scalar(
                    select(WhitelabelPartner)
                    .where(WhitelabelPartner.id == purpose_id)
                    .execution_options(skip_partner_scope=True)
                )
                if partner:
                    from app.services.franchise_billing import prompt_for_bot_token
                    await prompt_for_bot_token(partner)
            else:
                await notify_payment_success(user_id, result, purpose_id, amount=topup_amount)

    return processed
