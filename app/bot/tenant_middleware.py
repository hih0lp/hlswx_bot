from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services.tenant import enter_partner_scope, leave_partner_scope


class PartnerScopeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from app.bot.runtime import get_partner_for_bot

        bot = data.get("bot")
        partner = get_partner_for_bot(bot.id) if bot else None
        billing_status = getattr(partner, "franchise_billing_status", "") if partner else ""
        if partner and billing_status == "pending":
            return None
        if partner and billing_status == "suspended":
            user = getattr(event, "from_user", None)
            owner = user and user.id == partner.owner_telegram_id
            text = (event.text or "").strip() if isinstance(event, Message) else ""
            message_entry = isinstance(event, Message) and (text.startswith("/franchise") or text == "🏷 Франшиза")
            callback_entry = isinstance(event, CallbackQuery) and (event.data or "").startswith("franchise:")
            if not (owner and (message_entry or callback_entry)):
                if isinstance(event, Message):
                    await event.answer("Бот временно приостановлен из-за неоплаты франшизы. Владелец может открыть раздел «Франшиза» и возобновить тариф.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("Бот приостановлен из-за неоплаты франшизы", show_alert=True)
                return None
        tokens = enter_partner_scope(
            partner.id if partner else 0,
            partner.owner_telegram_id if partner else None,
            getattr(partner, "franchise_tier", "") if partner else "",
        )
        try:
            return await handler(event, data)
        finally:
            leave_partner_scope(tokens)
