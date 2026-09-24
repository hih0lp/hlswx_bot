"""Замечания заказчика от 20.09.2026 — по одному тесту на жалобу.

Замечания пришли скринами «как в боте / как на макете» и одной фразой про
интервал публикаций. Источник истины для них разный, и это важно:

1. Кнопки минут (интервал и тихий режим) — по **оригиналу** макета
   `wJbvRHNGmCH6Owl4OjJ7Ud`: там они стоят парами. В нашей копии они были
   сведены в один ряд нашей же правкой от 17.09.2026 — заказчик её отменил.
2. Карточка записи белого списка, список записей и экран «не удалось
   добавить» — по кадрам `338:661`, `338:725` и `340:677`. Каждый из них
   нарисован и в оригинале, и в копии одинаково.
3. «Баннеры не должны быть везде, баннер только там стоит, где в фигме» — в
   макете картинка есть ровно на одном кадре админки, `337:208`.

Кнопки проверяем разбором разметки, а не исходника: сетка — это ровно то, что
разъехалось, и структурная проверка по тексту файла её не видит.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin import home, whitelist
from app.handlers.admin.queue import SET_INTERVAL_CB, SET_SILENCE_CB, _choice_rows
from app.handlers.admin.whitelist import _render_wl_list, _wl_card
from app.models.entities import WhitelistEntry
from app.services import admin_ui, banners
from tests.test_admin_city_add_flow import ADMIN_ID, _Bot, _fake_callback, _fake_message


async def _noop(*args, **kwargs):
    return None


def _state():
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=ADMIN_ID, user_id=ADMIN_ID),
    )


def _grid(markup) -> list[list[str]]:
    return [[b.text for b in row] for row in markup.inline_keyboard]


# ------------------------------------------------- 1. минуты по две в ряд


def test_interval_choices_stand_in_pairs():
    """Кадр 341:1248 оригинала: «1|2», «3|4», «5» — а не пять в строку."""
    rows = _choice_rows(T.INTERVAL_CHOICES, 2, SET_INTERVAL_CB)
    assert [[b.text for b in row] for row in rows] == [
        ["1 мин", "2 мин"], ["3 мин", "4 мин"], ["5 мин"],
    ]


def test_silence_choices_stand_in_pairs():
    """Кадр 344:1284 оригинала: «10|20», «30|60»."""
    rows = _choice_rows(T.SILENCE_CHOICES, 20, SET_SILENCE_CB)
    assert [[b.text for b in row] for row in rows] == [
        ["10 мин", "20 мин"], ["30 мин", "60 мин"],
    ]


def test_chosen_value_stays_highlighted():
    """Заливка выбранного — единственное, что отличает варианты между собой."""
    rows = _choice_rows(T.INTERVAL_CHOICES, 3, SET_INTERVAL_CB)
    styles = {b.text: b.style for row in rows for b in row}
    assert styles["3 мин"] != styles["1 мин"]


# ------------------------------------------- 2. карточка записи, кадр 338:661


def _entry(**kw) -> WhitelistEntry:
    base = dict(
        id=12,
        telegram_id=1904406102,
        username="irina_sh75ui",
        city_keys="[]",
        chat_ids="[]",
        expires_at=None,
    )
    return WhitelistEntry(**(base | kw))


def test_wl_card_follows_338_661():
    """Без заголовка «⭐ Белый список» и без подписей «Города ·», «Чаты ·»."""
    text, _ = _wl_card(_entry())
    lines = text.split("\n")
    assert lines[0].startswith("👤 @irina_sh75ui · ID:")
    assert "⭐ Белый список" not in text
    assert lines[-3:] == ["🌍 Все города", "💬 Все чаты", "⏱ Навсегда"]


def test_wl_card_buttons_follow_338_661():
    """«Изменить» и «Удалить» в один ряд, возврат — на экран раздела."""
    _, markup = _wl_card(_entry())
    assert _grid(markup) == [
        [T.BTN_WL_EDIT, T.BTN_WL_DELETE],
        [T.BTN_WHITELIST],
    ]
    assert markup.inline_keyboard[0][0].callback_data == "adm:wl:edit:12"
    assert markup.inline_keyboard[-1][0].callback_data == "adm:wl"


def test_wl_card_shows_expired_status():
    """Срок вышел — кружок красный: значок и есть индикатор (правка task6)."""
    text, _ = _wl_card(_entry(expires_at=datetime.now(UTC) - timedelta(days=1)))
    assert T.WL_STATUS_EXPIRED in text


# --------------------------------------------- 3. список записей, кадр 338:725


async def test_wl_list_has_only_back_under_pager(world, bot):
    """Под пагинацией одна кнопка: «Найти» и «Добавить» живут на экране раздела."""
    async with SessionLocal() as session:
        session.add(_entry(id=None))
        await session.commit()
    text, markup = await _render_wl_list(0)
    assert text.startswith("<b>📋 Все записи</b>")
    grid = _grid(markup)
    assert grid[-1] == ["← Назад"]
    assert T.BTN_WL_FIND not in [b for row in grid for b in row]
    assert T.BTN_WL_ADD not in [b for row in grid for b in row]


# ------------------------------------- 4. «не удалось добавить», кадр 340:677


def test_add_exists_screen_shows_the_entry():
    """Одной строкой «уже в списке» экран не ограничивается — видно и охват."""
    text = T.WL_ADD_EXISTS.format(who="@irina_sh75ui", cities="Все города", term="Навсегда")
    assert text.split("\n") == [
        "<b>⚠️ Не удалось добавить</b>",
        "",
        "@irina_sh75ui уже есть в белом списке.",
        "",
        "👤 @irina_sh75ui",
        "🌍 Все города",
        "⏱ Навсегда",
    ]


# ------------------------------------------------------- 5. правка записи


async def test_edit_opens_the_wizard_on_the_existing_entry(world, bot, monkeypatch):
    """«Изменить» — это мастер добавления с уже отмеченным охватом записи."""
    monkeypatch.setattr("app.services.access.is_staff", lambda user_id: True)
    async with SessionLocal() as session:
        session.add(_entry(id=None, city_keys='["msk"]', chat_ids="[3]"))
        await session.commit()
        row = await session.scalar(
            select(WhitelistEntry).where(WhitelistEntry.telegram_id == 1904406102),
        )
        entry_id = row.id

    fake_bot, state = _Bot(), _state()
    await whitelist.admin_wl_edit(_fake_callback(fake_bot, f"adm:wl:edit:{entry_id}"), state)

    assert await state.get_state() == "AdminFlow:whitelist_cities"
    data = await state.get_data()
    assert data["wl_edit_id"] == entry_id
    assert data["wl_selected_cities"] == ["msk"]
    assert data["wl_selected_chats"] == [3]


async def test_edit_rewrites_the_entry_instead_of_adding_a_second(world, bot, monkeypatch):
    """Вторая строка на того же пользователя — это два разных охвата сразу.

    Какой из них подействует, зависело бы от порядка выборки, поэтому мастер
    правки обязан попасть в ту же запись, а не завести новую.
    """
    monkeypatch.setattr("app.services.access.is_staff", lambda user_id: True)
    monkeypatch.setattr("app.services.admin_audit.log_action", _noop)
    async with SessionLocal() as session:
        session.add(_entry(id=None, city_keys='["msk"]'))
        await session.commit()

    fake_bot, state = _Bot(), _state()
    await state.set_state("AdminFlow:whitelist_comment")
    await state.update_data(
        wl_edit_id=1,
        wl_tg_id=1904406102,
        wl_username="irina_sh75ui",
        wl_pending=False,
        wl_selected_cities=["msk", "spb"],
        wl_selected_chats=[],
        wl_expires_at=None,
    )
    await whitelist.admin_wl_save(_fake_message(fake_bot, "-"), state)

    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(WhitelistEntry).where(WhitelistEntry.telegram_id == 1904406102),
            )
        ).all()
    assert len(rows) == 1, "правка завела вторую запись вместо изменения первой"
    assert json.loads(rows[0].city_keys) == ["msk", "spb"]
    assert any("Запись обновлена" in text for text in fake_bot.sent)


async def test_add_after_edit_still_reports_adding(world, bot, monkeypatch):
    """`wl_edit_id` живёт в том же состоянии — без сброса добавление врало бы."""
    monkeypatch.setattr("app.services.access.is_staff", lambda user_id: True)
    fake_bot, state = _Bot(), _state()
    await state.update_data(wl_edit_id=42)

    await whitelist.admin_wl_add_start(_fake_callback(fake_bot, "adm:wl:add"), state)

    assert (await state.get_data())["wl_edit_id"] is None


# ------------------------------------------------------- 6. баннер не везде


def _keys_used(monkeypatch) -> list:
    """Какие баннеры запросили экраны панели за вызов."""
    seen = []

    async def spy(_message, key, _text, _markup=None, **kwargs):
        seen.append(key)

    monkeypatch.setattr(banners, "show_screen", spy)
    monkeypatch.setattr(banners, "show_prompt", spy)
    return seen


async def test_ordinary_screen_goes_without_a_banner(world, bot, monkeypatch):
    """Умолчание — экран без картинки: так нарисовано на всех кадрах, кроме одного."""
    seen = _keys_used(monkeypatch)
    await admin_ui.show(_fake_callback(_Bot(), "adm:wl"), "текст", None)
    await admin_ui.prompt(_fake_callback(_Bot(), "adm:wl:add"), "введите", None)
    assert seen == [banners.PLAIN, banners.PLAIN]
    assert banners.banner_path(banners.PLAIN) is None


async def test_only_the_panel_home_asks_for_the_banner(world, bot, monkeypatch):
    """Кадр `337:208` — единственный в админке с картинкой, он её и просит."""
    monkeypatch.setattr("app.services.access.is_admin", lambda user_id: True)
    seen = _keys_used(monkeypatch)
    await home.show_panel(_fake_callback(_Bot(), "adm:home"))
    assert seen == [banners.ADMIN]
    assert banners.banner_path(banners.ADMIN) is not None


def _bot_message(*, photo: bool):
    from aiogram.types import Chat, Message, PhotoSize, User

    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=User(id=1, is_bot=True, first_name="bot"),
        photo=[PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)] if photo else None,
    )


async def test_redraw_refuses_to_swap_photo_for_text():
    """Экран с баннером и без — сообщения разного рода, на месте не меняются.

    Проверка не про аккуратность: без этой отсечки попытка перерисовать
    текстовый экран баннером успевала залить файл на ~800 КБ и только потом
    получала отказ Telegram — и так на каждом возврате в админку.

    `message.bot` у этих фейков нет: дойди вызов до Telegram, тест упал бы
    исключением, а не вернул False.
    """
    assert await banners.redraw(_bot_message(photo=True), banners.PLAIN, "текст") is False
    assert await banners.redraw(_bot_message(photo=False), banners.ADMIN, "текст") is False


class _RecordingBot:
    """Телеграм, который только записывает, что его попросили сделать."""

    id = 1

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, method, *a, **kw):
        self.calls.append(type(method).__name__)
        return True

    async def send_message(self, chat_id, text=None, **kw):
        self.calls.append("send_message")
        return _bot_message(photo=False).as_(self)

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        self.calls.append("send_photo")
        return _bot_message(photo=True).as_(self)

    async def edit_message_text(self, text=None, **kw):
        self.calls.append("edit_message_text")
        return _bot_message(photo=False).as_(self)


async def test_screen_that_cannot_be_redrawn_replaces_the_old_one():
    """Экран сменил род — покинутый удаляем, а не копим в переписке.

    Это единственный переход, который в админке нельзя сделать правкой на
    месте: главный экран идёт с баннером, разделы — текстом. Без удаления
    каждый вход в раздел и возврат оставляли бы в чате мёртвый экран.
    """
    bot = _RecordingBot()
    banners.forget_screen(ADMIN_ID)
    await banners.show_screen(
        _bot_message(photo=True).as_(bot), banners.PLAIN, "раздел",
        edit=True, replace=True,
    )
    assert bot.calls.index("DeleteMessage") < bot.calls.index("send_message")


async def test_without_replace_the_old_screen_only_loses_buttons():
    """Пользовательская часть не тронута: там покинутый экран остаётся."""
    bot = _RecordingBot()
    banners.forget_screen(ADMIN_ID)
    await banners.show_screen(
        _bot_message(photo=True).as_(bot), banners.PLAIN, "экран", edit=True,
    )
    assert "DeleteMessage" not in bot.calls
    assert "EditMessageReplyMarkup" in bot.calls


def test_banner_is_requested_from_exactly_one_place():
    """Чтобы баннер не расползся обратно по разделам — сторож на исходники.

    Проверка структурная: экраны собираются inline в обработчиках, и вызвать
    их все без живого Telegram нельзя. А расползание уже случалось — до
    20.09.2026 баннер лепился на каждый экран панели.
    """
    from pathlib import Path

    admin_dir = Path(__file__).resolve().parents[1] / "app" / "handlers" / "admin"
    offenders = {
        path.name
        for path in admin_dir.glob("*.py")
        if "banner=True" in path.read_text(encoding="utf-8")
    }
    assert offenders == {"home.py"}, f"баннер просят и эти разделы: {sorted(offenders)}"
