"""Выбор чатов в белом списке: развилка и мультивыбор (ТЗ этапа 2, 6.3).

Кадры 338:537 и 338:724. По уточнению заказчика от 14.09.2026 сначала идёт
экран «Все чаты / Выбрать чаты», и только по второй кнопке открывается
мультивыбор — «такая же механика, как у пользователя, когда оформляет
подписку». Отличие от прежней реализации: зелёная «Готово» больше не висит
всегда, она появляется после первого отмеченного чата.
"""

from __future__ import annotations

from app.core import admin_texts as T
from app.handlers.admin.whitelist import (
    _wl_chats_keyboard,
    _wl_chats_mode_keyboard,
)
from app.keyboards.main import chats_keyboard
from app.states.admin import AdminFlow


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_mode_screen_offers_exactly_two_ways():
    markup = _wl_chats_mode_keyboard()
    assert _labels(markup) == [T.BTN_WL_CHATS_ALL, T.BTN_WL_CHATS_PICK, "← Назад"]
    assert _callbacks(markup)[:2] == ["adm:wl:chatmode:all", "adm:wl:chatmode:pick"]


def test_mode_screen_names_the_user_and_the_cities():
    text = T.WL_CHATS_MODE.format(who="@vip", cities="Все города")
    assert "@vip" in text
    assert "Все города" in text
    assert "Выберите чаты" in text


def test_done_appears_only_after_the_first_pick():
    """Как у подписчика: пустой выбор — кнопки нет."""
    chats = _fake_chats()

    empty = _labels(_wl_chats_keyboard(chats, set()))
    assert not [label for label in empty if label.startswith("✅ Готово")]

    picked = _labels(_wl_chats_keyboard(chats, {1}))
    assert "✅ Готово · 1 чат" in picked


def test_all_chats_button_left_the_multiselect():
    """Этот вариант выбирается развилкой — в мультивыборе он лишний."""
    callbacks = _callbacks(_wl_chats_keyboard(_fake_chats(), {1}))
    assert "adm:wl:chat:all" not in callbacks


def test_back_from_multiselect_returns_to_the_fork():
    callbacks = _callbacks(_wl_chats_keyboard(_fake_chats(), set()))
    assert callbacks[-1] == "adm:wl:chatmode"


def test_marks_match_the_subscriber_flow():
    """Галочка и пустой кружок — те же, что на шаге 4 у подписчика."""
    chats = _fake_chats()
    admin = _labels(_wl_chats_keyboard(chats, {1}))
    user = _labels(chats_keyboard(chats, {1}))

    assert admin[0].startswith("✅ ") and user[0].startswith("✅ ")
    assert admin[1].startswith("▫️ ") and user[1].startswith("▫️ ")


def test_fork_has_its_own_state():
    assert hasattr(AdminFlow, "whitelist_chats_mode")


class _Chat:
    def __init__(self, chat_id: int, username: str) -> None:
        self.id = chat_id
        self.telegram_username = username
        self.title = username
        self.topic = "Работа"
        self.network = "hammer"


def _fake_chats() -> list[_Chat]:
    return [_Chat(1, "group_a"), _Chat(2, "group_b")]
