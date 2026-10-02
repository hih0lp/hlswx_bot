"""Раздел «Города и чаты» (ТЗ этапа 2, 6.5).

Собран по кадрам макета: 368:406 (раздел) → 368:429 (список городов) →
368:506 (карточка города); добавление города 368:637 → 368:644 → 368:664,
добавление чата 368:550 → 368:592 → 368:575.

Два отступления от кадров, оба ради сохранения работающих функций: на первом
экране оставлена кнопка «Проверить чаты» (её обещал текст добавления чата, а
попасть было некуда), а в карточке города — тумблеры города и чата, иначе
отключить группу нечем.

Работает над теми же таблицами `cities` и `chats`, что заполняет сид: после
переезда управления в панель сид остаётся первичным наполнением и разовой
сверкой по перечню заказчика, а строки вне перечня не переписывает.
"""

from __future__ import annotations

import re
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import func, select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.common import LIST_PER_PAGE, _is_admin, leave_flow_if_escaped
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.models.entities import Chat, City
from app.services import access, admin_audit, admin_ui
from app.services.chat_network import chat_display_title, infer_chat_network
from app.services.textfmt import chats_label
from app.states.admin import AdminFlow

router = Router()

PLACES_CB = "adm:places"
CITIES_CB = "adm:places:cities"
CITY_CB = "adm:places:city:"
# Действия живут в своих неймспейсах, а не внутри `adm:places:city:`: пока они
# были «частным случаем» карточки города, отличить их можно было только
# отрицаниями в фильтре, и любая кнопка из истории чата, не попавшая под
# исключение, приводила к `int("add")` внутри карточки. Теперь пересечения нет.
CITY_ADD_CB = "adm:places:cityadd"
CITY_ADD_OK_CB = "adm:places:citysave"
CITY_TOGGLE_CB = "adm:places:toggle:"
CHAT_ADD_CB = "adm:places:chat:add:"
CHAT_TOGGLE_CB = "adm:places:chattoggle:"
# Удаление — «мягкое»: чат и город отключаются и пропадают из панели, а строки
# остаются, потому что на них ссылаются подписки и журнал публикаций.
CHAT_DELETE_CB = "adm:places:chatdel:"
CHAT_REMOVE_CB = "adm:places:chatrm:"
CITY_DELETE_CB = "adm:places:citydel:"
CITY_DELETE_OK_CB = "adm:places:citydelok:"
DELETE_BUTTONS_PER_ROW = 4

CITY_KEY_RE = re.compile(r"^[a-z0-9_]{2,32}$")

# Карточка города: `adm:places:city:<id>` и её страница `adm:places:city:<id>:<page>`.
CITY_CARD_RE = re.compile(r"^adm:places:city:\d+(:\d+)?$")

# Публичная группа в Telegram: «@name» либо «-100…» у супергрупп.
CHAT_ID_RE = re.compile(r"^-?\d{5,20}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z][A-Za-z0-9_]{4,31}$")

# Служебный ключ города собирается из названия: администратор его больше не
# вводит (в кадре 368:637 спрашивают только название), но в БД он нужен —
# на него ссылаются пакетные тарифы и записи белого списка.
TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _slug(label: str) -> str:
    out = "".join(TRANSLIT.get(ch, ch) for ch in label.strip().lower())
    out = re.sub(r"[^a-z0-9]+", "_", out).strip("_")
    return out[:32] or "city"


async def _city_key_for(session, label: str) -> str:
    """Свободный ключ для нового города: `nizhniy_novgorod`, при коллизии `_2`."""
    base = _slug(label)
    if not CITY_KEY_RE.match(base):
        base = f"c_{base}"[:32]
    key, suffix = base, 1
    while await session.scalar(select(City).where(City.key == key)):
        suffix += 1
        tail = f"_{suffix}"
        key = f"{base[:32 - len(tail)]}{tail}"
    return key


# ------------------------------------------------------------------- экраны


@router.callback_query(F.data == PLACES_CB)
async def admin_places(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 368:406 — раздел со счётчиками."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    async with SessionLocal() as session:
        cities_total = await session.scalar(
            select(func.count()).select_from(City).where(City.active.is_(True)),
        ) or 0
        chats_total = await session.scalar(
            select(func.count())
            .select_from(Chat)
            .join(City, City.id == Chat.city_id)
            .where(Chat.active.is_(True), City.active.is_(True)),
        ) or 0

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            # Одна колонка, как в разделе «Белый список» (заказчик 21.09.2026).
            [InlineKeyboardButton(text=T.BTN_CITY_ADD, callback_data=CITY_ADD_CB, style=STYLE_MAIN)],
            [InlineKeyboardButton(text=T.BTN_CITIES_ALL, callback_data=CITIES_CB, style=STYLE_PLAIN)],
            # Кадра под эту кнопку нет, но текст добавления чата её обещает.
            [InlineKeyboardButton(text=T.BTN_CHATS_CHECK, callback_data="adm:chats", style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(
        callback,
        T.PLACES.format(cities=cities_total, chats=chats_total),
        markup,
    )


async def _cities_screen(page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """Кадр 368:429 — список городов с числом чатов у каждого."""
    async with SessionLocal() as session:
        cities = list(
            (await session.scalars(select(City).where(City.active.is_(True)).order_by(City.id))).all(),
        )
        counts = dict(
            (
                await session.execute(
                    select(Chat.city_id, func.count())
                    .where(Chat.active.is_(True))
                    .group_by(Chat.city_id),
                )
            ).all(),
        )

    shown, page, pages = page_slice(cities, page, LIST_PER_PAGE)
    rows = [
        [
            InlineKeyboardButton(
                text=T.CITY_BUTTON.format(
                    label=city.label,
                    chats=chats_label(counts.get(city.id, 0)),
                ),
                callback_data=f"{CITY_CB}{city.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for city in shown
    ]
    pager = pager_row(f"{CITIES_CB}:", page, pages)
    if pager:
        rows.append(pager)
    # «Добавить» здесь нет: та же кнопка стоит на экране раздела (заказчик 21.09.2026).
    rows.append([admin_ui.back_button(PLACES_CB, T.BTN_BACK_TO_PLACES)])
    return T.CITIES, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith(CITIES_CB))
async def admin_cities(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    _, _, raw_page = callback.data[len(CITIES_CB):].partition(":")
    await admin_ui.show(callback, *await _cities_screen(int(raw_page or 0)))


def _chat_line(chat: Chat) -> str:
    """Чат обычным текстом; у группы с username название — ссылка на неё."""
    title = escape(chat_display_title(chat))
    username = (chat.telegram_username or "").lstrip("@")
    return f'<a href="https://t.me/{username}">{title}</a>' if username else title


async def _active_chats(session, city_id: int) -> list[Chat]:
    return list(
        (
            await session.scalars(
                select(Chat)
                .where(Chat.city_id == city_id, Chat.active.is_(True))
                .order_by(Chat.sort_order, Chat.id),
            )
        ).all(),
    )


async def _city_screen(city_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Кадр 368:506 — работающие чаты города списком со ссылками."""
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
        if city is None or not city.active:
            return None
        chats = await _active_chats(session, city_id)

    if chats:
        text = T.CITY_CARD.format(label=escape(city.label), chats="\n".join(_chat_line(c) for c in chats))
    else:
        text = T.CITY_CARD_EMPTY.format(label=escape(city.label))

    actions = [
        InlineKeyboardButton(text=T.BTN_CHAT_ADD, callback_data=f"{CHAT_ADD_CB}{city_id}", style=STYLE_PLAIN),
    ]
    if chats:
        actions.append(
            InlineKeyboardButton(
                text=T.BTN_CHAT_DELETE, callback_data=f"{CHAT_DELETE_CB}{city_id}", style=STYLE_PLAIN,
            ),
        )
    rows = [
        actions,
        [
            InlineKeyboardButton(
                text=T.BTN_CITY_DELETE, callback_data=f"{CITY_DELETE_CB}{city_id}", style=STYLE_DANGER,
            ),
        ],
        [admin_ui.back_button(CITIES_CB, T.BTN_BACK_TO_LISTS)],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _chat_delete_screen(city_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    """Чаты города по порядку и под ними кнопки «🗑 номер». None — чатов не осталось."""
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
        if city is None or not city.active:
            return None
        chats = await _active_chats(session, city_id)
    if not chats:
        return None

    listing = "\n".join(f"{number}. {_chat_line(chat)}" for number, chat in enumerate(chats, start=1))
    buttons = [
        InlineKeyboardButton(
            text=f"🗑 {number}", callback_data=f"{CHAT_REMOVE_CB}{chat.id}", style=STYLE_PLAIN,
        )
        for number, chat in enumerate(chats, start=1)
    ]
    rows = [
        buttons[i:i + DELETE_BUTTONS_PER_ROW] for i in range(0, len(buttons), DELETE_BUTTONS_PER_ROW)
    ]
    rows.append([admin_ui.back_button(f"{CITY_CB}{city_id}")])
    text = T.CHAT_DELETE.format(label=escape(city.label), chats=listing)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.regexp(CITY_CARD_RE))
async def admin_city(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    city_id = int(callback.data[len(CITY_CB):].partition(":")[0])
    rendered = await _city_screen(city_id)
    if rendered is None:
        await callback.answer("Город не найден", show_alert=True)
        return
    await callback.answer()
    await admin_ui.show(callback, *rendered, preview=False)


@router.callback_query(F.data.startswith(CHAT_DELETE_CB))
async def admin_chat_delete_pick(callback: CallbackQuery) -> None:
    """«Удалить чат» — тот же список, но с кнопками-корзинами под ним."""
    if not _is_admin(callback.from_user.id):
        return
    city_id = int(callback.data.rsplit(":", 1)[-1])
    rendered = await _chat_delete_screen(city_id)
    if rendered is None:
        await callback.answer("Чатов нет", show_alert=True)
        return
    await callback.answer()
    await admin_ui.show(callback, *rendered, preview=False)


@router.callback_query(F.data.startswith(CHAT_REMOVE_CB))
async def admin_chat_remove(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        chat = await session.scalar(select(Chat).where(Chat.id == chat_id))
        if chat is None:
            await callback.answer("Чат не найден", show_alert=True)
            return
        chat.active = False
        city_id = chat.city_id
        target = f"@{chat.telegram_username}" if chat.telegram_username else str(chat.telegram_chat_id)
        title = chat.title
        await session.commit()
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.CHAT_DELETE, target=target, detail=title,
    )
    await callback.answer("Чат удалён")
    # Остались чаты — остаёмся на экране удаления, чтобы убрать несколько подряд.
    rendered = await _chat_delete_screen(city_id) or await _city_screen(city_id)
    if rendered is None:
        rendered = await _cities_screen()
    await admin_ui.show(callback, *rendered, preview=False)


@router.callback_query(F.data.startswith(CITY_DELETE_CB))
async def admin_city_delete_ask(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    city_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
        if city is None or not city.active:
            await callback.answer("Город не найден", show_alert=True)
            return
        chats_count = len(await _active_chats(session, city_id))
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CITY_DELETE_OK, callback_data=f"{CITY_DELETE_OK_CB}{city_id}", style=STYLE_DANGER,
                ),
            ],
            [admin_ui.back_button(f"{CITY_CB}{city_id}", T.BTN_CANCEL)],
        ],
    )
    await callback.answer()
    await admin_ui.show(
        callback, T.CITY_DELETE_CONFIRM.format(label=escape(city.label), chats=chats_count), markup,
    )


@router.callback_query(F.data.startswith(CITY_DELETE_OK_CB))
async def admin_city_delete(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    city_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
        if city is None or not city.active:
            await callback.answer("Город уже удалён", show_alert=True)
            return
        for chat in await _active_chats(session, city_id):
            chat.active = False
        city.active = False
        key, label = city.key, city.label
        await session.commit()
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.CITY_DELETE, target=key, detail=label,
    )
    await callback.answer("Город удалён")
    await admin_ui.show(callback, *await _cities_screen())


# Кнопки старых сообщений («Отключить город», «💬 чат»): включать и отключать
# больше нельзя — просто открываем карточку города.
@router.callback_query(F.data.startswith(CITY_TOGGLE_CB))
async def admin_city_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    rendered = await _city_screen(int(callback.data.rsplit(":", 1)[-1]))
    await callback.answer()
    if rendered is not None:
        await admin_ui.show(callback, *rendered, preview=False)


@router.callback_query(F.data.startswith(CHAT_TOGGLE_CB))
async def admin_chat_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    chat_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        chat = await session.scalar(select(Chat).where(Chat.id == chat_id))
    await callback.answer()
    rendered = await _city_screen(chat.city_id) if chat else None
    if rendered is not None:
        await admin_ui.show(callback, *rendered, preview=False)


# -------------------------------------------------------- добавление города


def _back(cb: str, label: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(cb, label)]])


@router.callback_query(F.data == CITY_ADD_CB)
async def admin_city_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1, кадр 368:637 — название города."""
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.city_add)
    await callback.answer()
    await admin_ui.prompt(callback, T.CITY_ADD_ASK, _back(CITIES_CB))


@router.message(AdminFlow.city_add)
async def admin_city_add(message: Message, state: FSMContext) -> None:
    """Шаг 2, кадр 368:644 — подтверждение названия."""
    if not _is_admin(message.from_user.id):
        return
    if await leave_flow_if_escaped(message, state):
        return
    label = (message.text or "").strip()
    if not label or len(label) > 100:
        await admin_ui.show(message, T.CITY_ADD_BAD, _back(CITIES_CB), edit=False)
        return

    async with SessionLocal() as session:
        exists = await session.scalar(
            select(City).where(func.lower(City.label) == label.lower(), City.active.is_(True)),
        )
        if exists:
            await admin_ui.show(
                message, T.CITY_ADD_EXISTS.format(label=label), _back(CITIES_CB), edit=False,
            )
            return

    await state.set_state(AdminFlow.city_add_confirm)
    await state.update_data(city_label=label)
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CITY_ADD_CONFIRM, callback_data=CITY_ADD_OK_CB, style=STYLE_MAIN,
                ),
                InlineKeyboardButton(
                    text=T.BTN_CITY_ADD_EDIT, callback_data=CITY_ADD_CB, style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button(CITIES_CB)],
        ],
    )
    await admin_ui.show(message, T.CITY_ADD_CONFIRM.format(label=label), markup, edit=False)


@router.callback_query(F.data == CITY_ADD_OK_CB)
async def admin_city_add_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 3, кадр 368:664 — город создан."""
    if not _is_admin(callback.from_user.id):
        return
    label = (await state.get_data()).get("city_label")
    if not label:
        await callback.answer("Название потерялось, начните заново", show_alert=True)
        return

    async with SessionLocal() as session:
        # Ранее удалённый город с тем же названием возвращаем, а не заводим
        # второй: на его ключ ссылаются записи белого списка и тарифы.
        city = await session.scalar(select(City).where(func.lower(City.label) == label.lower()))
        if city is None:
            city = City(key=await _city_key_for(session, label), label=label, active=True)
            session.add(city)
        else:
            city.active = True
        await session.commit()
        city_id = city.id
        key = city.key

    await state.clear()
    await admin_audit.log_action(callback.from_user.id, admin_audit.CITY_ADD, target=key, detail=label)
    await callback.answer()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CHAT_ADD, callback_data=f"{CHAT_ADD_CB}{city_id}", style=STYLE_MAIN,
                ),
                InlineKeyboardButton(
                    text=T.BTN_CITY_OPEN, callback_data=f"{CITY_CB}{city_id}", style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button(PLACES_CB, T.BTN_BACK_TO_PLACES)],
        ],
    )
    await admin_ui.show(callback, T.CITY_ADDED.format(label=label), markup)


# ---------------------------------------------------------- добавление чата


@router.callback_query(F.data.startswith(CHAT_ADD_CB))
async def admin_chat_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаг 1, кадр 368:550 — название чата."""
    if not _is_admin(callback.from_user.id):
        return
    city_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
    if city is None:
        await callback.answer("Город не найден", show_alert=True)
        return
    await state.set_state(AdminFlow.chat_add)
    await state.update_data(chat_city_id=city_id)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.CHAT_ADD_ASK.format(city=city.label),
        _back(f"{CITY_CB}{city_id}"),
    )


@router.message(AdminFlow.chat_add)
async def admin_chat_add_title(message: Message, state: FSMContext) -> None:
    """Шаг 2, кадр 368:592 — username или ID группы."""
    if not _is_admin(message.from_user.id):
        return
    if await leave_flow_if_escaped(message, state):
        return
    data = await state.get_data()
    city_id = data.get("chat_city_id")
    back = _back(f"{CITY_CB}{city_id}")
    title = (message.text or "").strip()
    if not title or len(title) > 255:
        await admin_ui.show(message, T.CHAT_ADD_BAD, back, edit=False)
        return

    async with SessionLocal() as session:
        city = await session.scalar(select(City).where(City.id == city_id))
    await state.set_state(AdminFlow.chat_add_target)
    await state.update_data(chat_title=title)
    await admin_ui.prompt(
        message,
        T.CHAT_ADD_TARGET_ASK.format(city=city.label if city else "—", title=title),
        back,
    )


async def _resolve_target(raw: str) -> tuple[str | None, int | None, str]:
    """Resolve a chat and require the active bot to be an administrator there."""
    raw = raw.strip()
    if CHAT_ID_RE.match(raw):
        username, target = None, int(raw)
    elif USERNAME_RE.match(raw):
        username = raw.lstrip("@")
        target = f"@{username}"
    else:
        return None, None, "bad"

    from app.bot.runtime import get_bot

    bot = get_bot()
    try:
        tg = await bot.get_chat(target)
        me = await bot.get_me()
        member = await bot.get_chat_member(tg.id, me.id)
    except Exception as exc:
        return username, None, str(exc)[:120]
    if member.status not in {"administrator", "creator"}:
        return username, None, "not_admin"
    if tg.type == "channel" and not getattr(member, "can_post_messages", False):
        return username, None, "not_admin"
    return username, tg.id, ""


@router.message(AdminFlow.chat_add_target)
async def admin_chat_add(message: Message, state: FSMContext) -> None:
    """Шаг 3, кадр 368:575 — группа добавлена."""
    if not _is_admin(message.from_user.id):
        return
    if await leave_flow_if_escaped(message, state):
        return
    data = await state.get_data()
    city_id = data.get("chat_city_id")
    title = data.get("chat_title") or ""
    back = _back(f"{CITY_CB}{city_id}")

    username, chat_id, error = await _resolve_target(message.text or "")
    if error == "bad":
        await admin_ui.show(message, T.CHAT_ADD_TARGET_BAD, back, edit=False)
        return
    if error == "not_admin":
        await admin_ui.show(message, T.CHAT_ADD_BOT_NOT_ADMIN, back, edit=False)
        return

    async with SessionLocal() as session:
        if username:
            exists = await session.scalar(
                select(Chat).where(func.lower(Chat.telegram_username) == username.lower()),
            )
        else:
            exists = await session.scalar(select(Chat).where(Chat.telegram_chat_id == chat_id))
        if exists is not None and exists.active:
            await admin_ui.show(
                message,
                T.CHAT_ADD_EXISTS.format(username=username or chat_id),
                back,
                edit=False,
            )
            return

        city = await session.scalar(select(City).where(City.id == city_id))
        last_order = await session.scalar(
            select(func.coalesce(func.max(Chat.sort_order), 0)).where(Chat.city_id == city_id),
        )
        new_order = int(last_order or 0) + 1
        if exists is not None:
            # Ранее удалённый чат подключаем заново: на строку ссылается журнал публикаций.
            exists.city_id = city_id
            exists.title = title
            if username:
                exists.telegram_username = username
            if chat_id:
                exists.telegram_chat_id = chat_id
            exists.network = infer_chat_network(exists.telegram_username or "")
            exists.sort_order = new_order
            exists.active = True
        else:
            session.add(
                Chat(
                    city_id=city_id,
                    title=title,
                    telegram_username=username or "",
                    telegram_chat_id=chat_id,
                    network=infer_chat_network(username or ""),
                    sort_order=new_order,
                    active=True,
                ),
            )
        await session.commit()
        city_label = city.label if city else "—"

    await state.clear()
    target = f"@{username}" if username else f"<code>{chat_id}</code>"
    await admin_audit.log_action(
        message.from_user.id, admin_audit.CHAT_ADD, target=target, detail=f"{city_label}: {title}",
    )
    await _notify_new_chat(message.from_user, title=title, target=target, city=city_label)

    rows = []
    if username:
        rows.append([
            InlineKeyboardButton(text=T.BTN_GROUP_OPEN, url=f"https://t.me/{username}"),
        ])
    rows.append([
        InlineKeyboardButton(
            text=T.BTN_CITY_BACK, callback_data=f"{CITY_CB}{city_id}", style=STYLE_MAIN,
        ),
    ])
    text = (
        T.CHAT_ADDED.format(city=city_label, title=title, target=target)
        if chat_id
        else T.CHAT_ADDED_NO_ID.format(
            city=city_label, title=title, target=target, error=error or "неизвестно",
        )
    )
    await admin_ui.show(message, text, InlineKeyboardMarkup(inline_keyboard=rows), edit=False)


async def _notify_new_chat(actor, *, title: str, target: str, city: str) -> None:
    """Уведомление администраторам о подключении новой группы (ТЗ 6.5)."""
    from app.bot.runtime import get_bot

    bot = get_bot()
    who = f"@{actor.username}" if actor.username else str(actor.id)
    text = T.CHAT_ADDED_NOTICE.format(title=title, target=target, city=city, actor=who)
    for staff_id in await access.staff_ids():
        if staff_id == actor.id:
            continue
        try:
            await bot.send_message(staff_id, text)
        except Exception:
            pass
