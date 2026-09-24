"""Публикация напрямую нашим ботом — резерв, когда релей Hammer/W недоступен.

Посты, ушедшие этим путём, мы можем редактировать и закрывать сами (ТЗ 6.8);
у постов, ушедших релеем, автор — чужой бот, и правка возможна только на его стороне.
"""

from __future__ import annotations

import logging

from aiogram import Bot

from app.services.hammer_relay import RelayResult

logger = logging.getLogger("direct_publish")

DELIVERY_DIRECT = "direct"
DELIVERY_RELAY = "relay"


async def direct_publish(
    bot: Bot,
    *,
    text: str,
    contact: str,
    telegram_chat_id: int,
    photo_url: str | None = None,
    pin: bool = False,
) -> RelayResult:
    from app.services.publish import render_post

    body = render_post(text, contact)
    try:
        if photo_url:
            message = await bot.send_photo(telegram_chat_id, photo_url, caption=body)
        else:
            message = await bot.send_message(telegram_chat_id, body)
    except Exception as exc:
        logger.warning("Direct publish failed chat_id=%s: %s", telegram_chat_id, exc)
        return RelayResult(ok=False, error=str(exc)[:300], network=DELIVERY_DIRECT)

    if pin:
        try:
            await bot.pin_chat_message(telegram_chat_id, message.message_id, disable_notification=True)
        except Exception:
            logger.warning("Pin failed chat_id=%s message_id=%s", telegram_chat_id, message.message_id)

    return RelayResult(ok=True, message_id=message.message_id, network=DELIVERY_DIRECT)


async def direct_edit(
    bot: Bot,
    *,
    telegram_chat_id: int,
    message_id: int,
    text: str,
    contact: str,
    has_photo: bool = False,
) -> tuple[bool, str]:
    from app.services.publish import render_post

    body = render_post(text, contact)
    try:
        if has_photo:
            await bot.edit_message_caption(
                chat_id=telegram_chat_id, message_id=message_id, caption=body,
            )
        else:
            await bot.edit_message_text(body, chat_id=telegram_chat_id, message_id=message_id)
    except Exception as exc:
        logger.warning("Direct edit failed chat_id=%s message_id=%s: %s", telegram_chat_id, message_id, exc)
        return False, str(exc)[:300]
    return True, ""


async def direct_close(
    bot: Bot,
    *,
    telegram_chat_id: int,
    message_id: int,
    text: str,
    contact: str,
    has_photo: bool = False,
) -> tuple[bool, str]:
    """Зачеркнуть объявление, пометив его закрытым."""
    from app.services.publish import render_post

    body = f"<s>{render_post(text, contact)}</s>\n\n🔒 Объявление закрыто"
    return await direct_edit(
        bot,
        telegram_chat_id=telegram_chat_id,
        message_id=message_id,
        text=body,
        contact="",
        has_photo=has_photo,
    )
