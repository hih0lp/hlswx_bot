"""Переходы между экранами — правило заказчика от 15.09.2026, уточнённое 16.09.

Как в @HammerPaySystemBot: новый раздел (команда или кнопка меню) уходит новым
сообщением, а у покинутого экрана просто пропадают inline-кнопки; нажатие
внутри раздела перерисовывает тот же экран.

Шаги с вводом текста подчиняются тому же правилу: пришли на шаг кликом —
правим на месте, пришли после введённого пользователем текста — шлём новым
сообщением, чтобы просьба осталась над ответом. Тесты ниже фиксируют обе
половины правила.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.types import Chat, Message, User

from app.services import banners

CHAT_ID = 777
ADMIN_DIR = Path(__file__).resolve().parents[1] / "app" / "handlers" / "admin"

# Экраны с «ASK» в имени, на которых ничего не вводят: там выбор кнопкой.
BUTTON_ASK_SCREENS = {"WL_TERM_ASK"}


class ScreenBot:
    """Телеграм в объёме, который нужен экранам: отправка, правка, снятие кнопок."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.markup_dropped: list[int] = []
        self._next_id = 100

    async def send_message(self, chat_id, text=None, **kwargs):
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": self._next_id})
        return SimpleNamespace(message_id=self._next_id, photo=None)

    async def send_photo(self, chat_id, photo, caption=None, **kwargs):
        return await self.send_message(chat_id, caption, **kwargs)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kwargs):
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return SimpleNamespace(message_id=message_id)

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None):
        assert reply_markup is None
        self.markup_dropped.append(message_id)


def _message(bot: ScreenBot, *, from_bot: bool, message_id: int = 0) -> Message:
    """Сообщение, по которому экран решает, править ему или слать новое."""
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=1 if from_bot else 2, is_bot=from_bot, first_name="Тест"),
    ).as_(bot)


@pytest.fixture
def screen_bot(monkeypatch):
    """Экраны без баннера: картинка к правилу переходов отношения не имеет."""
    monkeypatch.setattr(banners, "banner_path", lambda key: None)
    bot = ScreenBot()
    banners.remember_screen(CHAT_ID, 42)
    yield bot
    banners.forget_screen(CHAT_ID)


async def test_menu_button_opens_section_with_a_new_message(screen_bot):
    """Кнопка меню приходит сообщением пользователя — это новый раздел."""
    await banners.show_screen(
        _message(screen_bot, from_bot=False), banners.PROFILE, "Профиль", None, edit=True,
    )

    assert len(screen_bot.sent) == 1
    assert screen_bot.edited == []
    assert screen_bot.markup_dropped == [42], "у покинутого экрана должны пропасть кнопки"


async def test_click_inside_section_redraws_the_same_message(screen_bot):
    """Клик по inline-кнопке даёт сообщение бота — его и перерисовываем."""
    await banners.show_screen(
        _message(screen_bot, from_bot=True, message_id=42),
        banners.PROFILE,
        "Профиль",
        None,
        edit=True,
    )

    assert screen_bot.sent == []
    assert [m["message_id"] for m in screen_bot.edited] == [42]
    assert screen_bot.markup_dropped == []


async def test_prompt_after_a_click_redraws_the_same_message(screen_bot):
    """Правка от 16.09: выбор кнопкой (например, города) остаётся на месте."""
    await banners.show_prompt(
        _message(screen_bot, from_bot=True, message_id=42),
        banners.AD,
        "Пришлите пример объявления",
        None,
        edit=True,
    )

    assert screen_bot.sent == []
    assert [m["message_id"] for m in screen_bot.edited] == [42]
    assert screen_bot.markup_dropped == []


async def test_prompt_after_user_input_goes_as_a_new_message(screen_bot):
    """А шаг следом за введённым текстом не затирает просьбу над ответом."""
    await banners.show_prompt(
        _message(screen_bot, from_bot=False),
        banners.AD,
        "Пришлите контакт",
        None,
    )

    assert len(screen_bot.sent) == 1
    assert screen_bot.edited == []
    assert screen_bot.markup_dropped == [42]


async def test_new_screen_is_remembered_for_the_next_click(screen_bot):
    """Следующий клик должен перерисовывать уже новый экран, а не прошлый."""
    await banners.show_new_screen(
        _message(screen_bot, from_bot=False), banners.PROFILE, "Профиль", None,
    )
    sent_id = screen_bot.sent[0]["message_id"]

    await banners.show_new_screen(
        _message(screen_bot, from_bot=False), banners.RULES, "Правила", None,
    )

    assert screen_bot.markup_dropped == [42, sent_id]


def test_panel_prompts_do_not_redraw_the_previous_screen():
    """Экраны панели с вводом текста идут через `admin_ui.prompt`."""
    pattern = re.compile(r"admin_ui\.show\((?:[^()]|\([^()]*\))*?T\.(\w*ASK\w*)", re.S)
    offenders: dict[str, list[str]] = {}
    for path in ADMIN_DIR.glob("*.py"):
        found = [
            name for name in pattern.findall(path.read_text(encoding="utf-8"))
            if name not in BUTTON_ASK_SCREENS
        ]
        if found:
            offenders[path.name] = found
    assert not offenders, f"эти экраны просят ввод, но правят прошлое сообщение: {offenders}"
