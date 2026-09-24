"""Сообщение нескольким получателям (ТЗ этапа 2, 6.11).

В макете кнопка «Нескольким пользователям» есть, а вела она на рассылку всем,
кто нажимал /start. Здесь проверяем сам список получателей и то, что рассылка
всем осталась отдельной механикой.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.session import SessionLocal
from app.handlers.admin.messages import (
    MANY_DROP_CB,
    _find_user,
    _many_screen,
    _who,
)
from app.models.entities import User
from app.services.admin_broadcast import broadcast_to_users, send_to_ids


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def _user(telegram_id: int, username: str | None) -> User:
    async with SessionLocal() as session:
        row = User(telegram_id=telegram_id, username=username, full_name="Тест")
        session.add(row)
        await session.commit()
        return row


async def test_recipient_found_by_username_case_insensitive():
    await _user(501, "Manager")
    found = await _find_user("@manager")
    assert found is not None
    assert found.telegram_id == 501


async def test_recipient_found_by_telegram_id():
    await _user(502, None)
    found = await _find_user("502")
    assert found is not None
    assert found.telegram_id == 502


async def test_unknown_recipient_is_not_invented():
    assert await _find_user("@nobody") is None


async def test_who_falls_back_to_id_without_username():
    user = await _user(503, None)
    assert _who(user) == "503"


async def test_list_screen_offers_removal_per_recipient():
    recipients = [{"id": 11, "who": "@one"}, {"id": 22, "who": "@two"}]
    text, markup = _many_screen(recipients)

    assert "Получателей: <b>2</b>" in text
    assert "@one" in text and "@two" in text
    assert f"{MANY_DROP_CB}11" in _callbacks(markup)
    assert f"{MANY_DROP_CB}22" in _callbacks(markup)
    assert "➡️ Далее" in _labels(markup)


async def test_send_to_ids_touches_only_the_chosen(bot):
    await _user(601, "a")
    await _user(602, "b")
    await _user(603, "c")

    stats = await send_to_ids(bot, "привет", [601, 603], delay_sec=0)

    assert stats == {"total": 2, "sent": 2, "blocked": 0, "failed": 0}
    assert sorted(item["chat_id"] for item in bot.sent) == [601, 603]


async def test_broadcast_still_reaches_everyone(bot):
    """Рассылка всем осталась отдельной механикой поверх send_to_ids."""
    await _user(701, "a")
    await _user(702, "b")

    stats = await broadcast_to_users(bot, "всем", delay_sec=0)

    async with SessionLocal() as session:
        total = len((await session.scalars(select(User.id))).all())
    assert stats["total"] == total == 2
    assert stats["sent"] == 2


async def test_blocked_recipient_does_not_break_the_rest(bot):
    from aiogram.exceptions import TelegramForbiddenError

    await _user(801, "a")
    await _user(802, "b")

    original = bot.send_message

    async def _send(chat_id, text=None, **kwargs):
        if chat_id == 801:
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        return await original(chat_id, text, **kwargs)

    bot.send_message = _send
    stats = await send_to_ids(bot, "привет", [801, 802], delay_sec=0)

    assert stats["blocked"] == 1
    assert stats["sent"] == 1
