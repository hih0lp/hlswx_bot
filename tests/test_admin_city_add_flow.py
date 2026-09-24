"""Добавление города из панели: маршрутизация и сам сценарий (ТЗ этапа 2, 6.5).

Заказчик сообщил, что кнопка «Добавить город» не работает технически
(task6.md от 17.09.2026). Существующие тесты раздела проверяли только хелперы
— транслитерацию ключа и разбор username чата, — а сама цепочка
«кнопка → ввод названия → подтверждение → город в базе» не проверялась ничем.

Первый тест здесь про маршрутизацию: он спрашивает у роутера панели, какой
обработчик получит каждый `callback_data` раздела. Пока действия добавления
жили внутри неймспейса карточки города (`adm:places:city:add`), отличить их от
`adm:places:city:<id>` можно было только отрицаниями в фильтре, а промах
отрицания означал `int("add")` внутри карточки — исключение без `answer()`,
то есть вечно крутящаяся кнопка у администратора.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User
from sqlalchemy import select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin import admin_router, places
from app.models.entities import City

ADMIN_ID = 777


def _callback(data: str) -> CallbackQuery:
    user = User(id=ADMIN_ID, is_bot=False, first_name="admin")
    chat = Chat(id=ADMIN_ID, type="private")
    message = Message(message_id=1, date=datetime.now(UTC), chat=chat, from_user=user)
    return CallbackQuery(
        id="1", from_user=user, chat_instance="test", data=data, message=message,
    )


async def _route(data: str) -> str | None:
    """Имя обработчика, который аиограм выберет для этого callback_data."""
    event = _callback(data)
    for section in admin_router.sub_routers:
        for handler in section.callback_query.handlers:
            matched, _ = await handler.check(event)
            if matched:
                return handler.callback.__name__
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (places.PLACES_CB, "admin_places"),
        (places.CITIES_CB, "admin_cities"),
        (f"{places.CITIES_CB}:2", "admin_cities"),
        (places.CITY_ADD_CB, "admin_city_add_start"),
        (places.CITY_ADD_OK_CB, "admin_city_add_save"),
        (f"{places.CITY_CB}7", "admin_city"),
        (f"{places.CITY_CB}7:1", "admin_city"),
        (f"{places.CITY_TOGGLE_CB}7", "admin_city_toggle"),
        (f"{places.CHAT_ADD_CB}7", "admin_chat_add_start"),
        (f"{places.CHAT_TOGGLE_CB}7", "admin_chat_toggle"),
    ],
)
async def test_every_button_of_the_section_reaches_its_own_handler(data, expected):
    assert await _route(data) == expected


@pytest.mark.asyncio
async def test_card_handler_never_sees_a_non_numeric_id():
    """Карточка города разбирает id числом — ей нельзя отдавать ничего иного."""
    for data in (places.CITY_ADD_CB, places.CITY_ADD_OK_CB, places.CITIES_CB):
        assert await _route(data) != "admin_city"


class _Bot:
    """Минимальный Telegram для баннерных экранов панели."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._next_id = 100

    async def send_message(self, chat_id, text=None, **kwargs):
        self._next_id += 1
        self.sent.append(text or "")
        return SimpleNamespace(message_id=self._next_id)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        return await self.send_message(chat_id, caption, **kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return None


def _fake_message(bot: _Bot, text: str | None = None):
    return SimpleNamespace(
        bot=bot,
        text=text,
        chat=SimpleNamespace(id=ADMIN_ID),
        from_user=SimpleNamespace(id=ADMIN_ID, username="admin", is_bot=False),
        answer=bot.send_message,
    )


def _fake_callback(bot: _Bot, data: str):
    """Клик по кнопке панели. Поля сообщения продублированы: `admin_ui` отличает
    callback от сообщения через `isinstance`, а фейк под эту проверку не подходит.
    """
    fake = _fake_message(bot)
    fake.data = data
    fake.message = _fake_message(bot)
    fake.answer = _noop
    return fake


@pytest.fixture
def _staff(monkeypatch):
    monkeypatch.setattr("app.services.access.is_staff", lambda user_id: True)


@pytest.mark.asyncio
async def test_city_is_created_end_to_end(_staff, monkeypatch):
    """Кнопка → название → подтверждение → город в базе."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    monkeypatch.setattr("app.services.admin_audit.log_action", _noop)

    bot = _Bot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )

    callback = _fake_callback(bot, places.CITY_ADD_CB)
    await places.admin_city_add_start(callback, state)
    assert await state.get_state() == "AdminFlow:city_add"
    assert any("Введите название города" in text for text in bot.sent)

    await places.admin_city_add(_fake_message(bot, "Тула"), state)
    assert (await state.get_data())["city_label"] == "Тула"

    callback.data = places.CITY_ADD_OK_CB
    await places.admin_city_add_save(callback, state)

    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.label == "Тула"))
    assert city is not None, "город не создался"
    assert city.key == "tula"
    assert city.active is True
    assert await state.get_state() is None


@pytest.mark.asyncio
async def test_command_does_not_become_a_city_name(_staff, monkeypatch):
    """`/admin` во время ввода — это выход из шага, а не город с таким названием."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    monkeypatch.setattr("app.handlers.admin.home.show_panel", _noop)

    bot = _Bot()
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )
    await state.set_state("AdminFlow:city_add")

    await places.admin_city_add(_fake_message(bot, "/admin"), state)

    async with SessionLocal() as session:
        assert await session.scalar(select(City).where(City.label == "/admin")) is None
    assert await state.get_state() is None


def test_add_button_fits_a_half_row():
    """Кнопка стоит рядом с «Все города» — «Добавить город» обрезалось."""
    assert len(T.BTN_CITY_ADD) <= 14, f"{T.BTN_CITY_ADD!r} — {len(T.BTN_CITY_ADD)} знаков"
    assert len(T.BTN_CITY_ADD_CONFIRM) <= 14
    assert len(T.BTN_CITY_OPEN) <= 14


async def _noop(*args, **kwargs):
    return None
