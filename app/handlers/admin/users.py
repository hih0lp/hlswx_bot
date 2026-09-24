"""Раздел «Пользователи» админ-панели.

Кадры макета: `349:564` — вход в раздел со сводкой, `349:954` — поиск,
`349:1025` — постраничный список, `349:967` — карточка пользователя.

Это **не** самостоятельный раздел панели, хотя 18.09.2026 мы сделали его
таким и завели кнопку в главном меню. Ошибка была в том, что макет читался по
кадрам, а связи между ними — 153 коннектора на странице — не читались вовсе.
Стрелки говорят однозначно:

    349:311 Управление → 349:522 Белый список → [🔎 Найти пользователя]
        → 349:564 → 349:954 поиск / 349:1025 список → 349:967 карточка

То есть это ветка поиска пользователя внутри белого списка, и ходят в неё из
«Управления». В меню панели её нет — как нет её и в перечне разделов ТЗ 6.1.
Поэтому кнопка возврата ведёт на `adm:manage:wl`, а не в панель. Разбор —
в docs/figma_diff_2026-09-20.md.

Зачем ветка нужна: из панели белый список ищут по существующим записям
(`338:654`), а отсюда — по всем пользователям бота, чтобы найденного добавить;
у карточки `349:967` для этого и стоит кнопка «⭐ Белый список».

«Активен» значит «есть действующая подписка»: на кадре `349:564` счётчик
активных совпадает с активными подписками из сводки панели.
"""

from __future__ import annotations

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
from app.handlers.admin.common import LIST_PER_PAGE, _is_admin
from app.handlers.admin.messages import TO_CB
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.models.entities import (
    Chat,
    Publication,
    Subscription,
    SubscriptionChat,
    SubscriptionStatus,
    User,
)
from app.services import admin_ui
from app.services.textfmt import moment_label
from app.states.admin import AdminFlow

router = Router()

USERS_CB = "adm:users"
FIND_CB = "adm:users:find"
LIST_CB = "adm:users:list:"      # + <страница>
CARD_CB = "adm:users:card:"      # + <id пользователя>


async def _active_user_ids(session) -> set[int]:
    """Кто сейчас с действующей подпиской — по ним считается «Активных»."""
    rows = await session.scalars(
        select(Subscription.user_id).where(Subscription.status == SubscriptionStatus.active),
    )
    return set(rows.all())


def _who(user: User) -> str:
    return f"@{user.username}" if user.username else str(user.telegram_id)


def user_label(user: User, *, active: bool, subscriptions: int) -> str:
    """Подпись записи в списке — кадр 349:1025, три строки."""
    state = T.USER_STATE_ACTIVE if active else T.USER_STATE_INACTIVE
    return f"#{user.id} · {_who(user)}\n{state}\nПодписок: {subscriptions}"


async def _counts(session, user: User) -> tuple[int, int, int]:
    """Подписки, публикации и города пользователя — три счётчика карточки."""
    subscriptions = await session.scalar(
        select(func.count()).select_from(Subscription).where(Subscription.user_id == user.id),
    ) or 0
    publications = await session.scalar(
        select(func.count()).select_from(Publication).where(Publication.user_id == user.id),
    ) or 0
    # Города считаем через чаты подписок: собственного поля «город» у
    # пользователя нет, а подписка всегда привязана к чатам одного города.
    cities = await session.scalar(
        select(func.count(func.distinct(Chat.city_id)))
        .select_from(Subscription)
        .join(SubscriptionChat, SubscriptionChat.subscription_id == Subscription.id)
        .join(Chat, Chat.id == SubscriptionChat.chat_id)
        .where(Subscription.user_id == user.id),
    ) or 0
    return subscriptions, publications, cities


# --------------------------------------------------------------- вход в раздел

@router.callback_query(F.data == USERS_CB)
async def admin_users(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 349:564."""
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(User)) or 0
        active = len(await _active_user_ids(session))

    # Кадр 349:564 ставит их парой, но «🔎 Найти пользователя» — это 21
    # знакоместо при 16 доступных в половину ряда. Тот же случай, что в белом
    # списке (338:453): в боте по одной в ряд, пару в макете разводим.
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_USER_FIND, callback_data=FIND_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_USERS_ALL, callback_data=f"{LIST_CB}0", style=STYLE_PLAIN)],
            # Возврат на «Белый список» из «Управления» — туда, откуда сюда и
            # приходят по стрелке 349:522 → 349:564. До 20.09.2026 тут стояло
            # «← В админку»: раздел ошибочно висел пунктом главного меню.
            [admin_ui.back_button("adm:manage:wl")],
        ],
    )
    await admin_ui.show(callback, T.USERS.format(total=total, active=active), markup)


# ---------------------------------------------------------------------- поиск

@router.callback_query(F.data == FIND_CB)
async def admin_users_find(callback: CallbackQuery, state: FSMContext) -> None:
    """Кадр 349:954."""
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.users_find)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.USER_FIND_ASK,
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(USERS_CB)]]),
    )


@router.message(AdminFlow.users_find)
async def admin_users_found(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    raw = (message.text or "").strip().lstrip("@")
    async with SessionLocal() as session:
        if raw.isdigit():
            user = await session.scalar(select(User).where(User.telegram_id == int(raw)))
        else:
            user = await session.scalar(select(User).where(func.lower(User.username) == raw.lower()))
        if user is None:
            await admin_ui.show(
                message,
                T.USER_NOT_FOUND.format(who=f"@{raw}"),
                InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(USERS_CB)]]),
                edit=False,
            )
            return
        text, markup = await _card(session, user)
    await admin_ui.show(message, text, markup, edit=False)


# --------------------------------------------------------------------- список

@router.callback_query(F.data.startswith(LIST_CB))
async def admin_users_list(callback: CallbackQuery) -> None:
    """Кадр 349:1025 — записи кнопками в три строки, как в макете."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    raw_page = callback.data.rsplit(":", 1)[-1]

    async with SessionLocal() as session:
        rows = list((await session.scalars(select(User).order_by(User.id))).all())
        active = await _active_user_ids(session)
        subs_by_user = dict(
            (
                await session.execute(
                    select(Subscription.user_id, func.count())
                    .where(Subscription.status == SubscriptionStatus.active)
                    .group_by(Subscription.user_id),
                )
            ).all(),
        )

    if not rows:
        await admin_ui.show(
            callback,
            T.USERS_LIST_EMPTY,
            InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(USERS_CB)]]),
        )
        return

    shown, page, pages = page_slice(rows, int(raw_page), LIST_PER_PAGE)
    buttons = [
        [
            InlineKeyboardButton(
                text=user_label(
                    user,
                    active=user.id in active,
                    subscriptions=subs_by_user.get(user.id, 0),
                ),
                callback_data=f"{CARD_CB}{user.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for user in shown
    ]
    pager = pager_row(LIST_CB, page, pages)
    if pager:
        buttons.append(pager)
    buttons.append([admin_ui.back_button(USERS_CB)])
    await admin_ui.show(
        callback,
        T.USERS_LIST.format(total=len(rows)),
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ------------------------------------------------------------------- карточка

async def _card(session, user: User) -> tuple[str, InlineKeyboardMarkup]:
    """Кадр 349:967."""
    active = user.id in await _active_user_ids(session)
    subscriptions, publications, cities = await _counts(session, user)
    text = T.USER_CARD.format(
        who=_who(user),
        telegram_id=user.telegram_id,
        status=T.USER_STATUS_ACTIVE if active else T.USER_STATUS_INACTIVE,
        subscriptions=subscriptions,
        publications=publications,
        cities=cities,
        last_seen=moment_label(user.last_seen_at),
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_USER_WL,
                    callback_data="adm:wl:add",
                    style=STYLE_PLAIN,
                ),
                InlineKeyboardButton(
                    text=T.BTN_USER_WRITE,
                    callback_data=f"{TO_CB}{user.telegram_id}",
                    style=STYLE_MAIN,
                ),
            ],
            [admin_ui.back_button(USERS_CB)],
        ],
    )
    return text, markup


@router.callback_query(F.data.startswith(CARD_CB))
async def admin_users_card(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    user_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        if user is None:
            await callback.answer("Пользователь не найден", show_alert=True)
            return
        text, markup = await _card(session, user)
    await admin_ui.show(callback, text, markup)
