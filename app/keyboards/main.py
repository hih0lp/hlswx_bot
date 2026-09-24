"""Клавиатуры по макету Figma (ТЗ 6.9, палитра и пагинация — ТЗ этапа 2, 6.8).

Reply-клавиатура — только главное меню (основные действия), всё остальное
inline-кнопками внутри сообщения: так устроен макет и так просил заказчик
в комментариях к нему.

Цвета берём не наугад: в макете обычная кнопка белая, а заливкой помечено
только выбранное или активное состояние. Роли вынесены в `keyboards.style`,
`ButtonStyle` здесь напрямую не используется.

Списки, которые могут вырасти (города, чаты, подписки), листаются по кругу —
`keyboards.pagination`.
"""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.core.texts import (
    ADMIN_BTN,
    BTN_BACK,
    BTN_CLOSE_AD,
    BTN_DONE,
    BTN_EDIT_TEXT,
    BTN_FRANCHISE,
    BTN_HOME,
    BTN_INTEGRATION,
    BTN_MYSUBS,
    BTN_PROFILE,
    BTN_PUBLISH,
    BTN_RETRY_PAY,
    BTN_RULES,
    BTN_START,
    BTN_SUBSCRIPTION,
    BTN_TOPUP,
)
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_ACTIVE, STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.services import access
from app.services.chat_network import chat_display_title
from app.services.pricing import posts_volume_button_label
from app.services.textfmt import chats_label

HOME_CB = "menu:home"

# Сколько элементов помещается на странице списка (ТЗ 6.8, круговая пагинация).
CITIES_PER_PAGE = 8
CHATS_PER_PAGE = 5
SUBS_PER_PAGE = 5


# --------------------------------------------------------------------- reply

def reply_main_keyboard(*, is_admin: bool = False, has_subscription: bool = False) -> ReplyKeyboardMarkup:
    """Главное меню — фрейм «Home».

    Без подписки главное действие — «Начать оформление», с подпиской —
    «Опубликовать». Пакет, Подключить и Платформа из меню убраны по макету;
    сами разделы остались доступны по командам.
    """
    main_btn = BTN_PUBLISH if has_subscription else BTN_START
    rows = [
        [KeyboardButton(text=main_btn, style=STYLE_MAIN)],
        [
            KeyboardButton(text=BTN_PROFILE, style=STYLE_PLAIN),
            KeyboardButton(text=BTN_SUBSCRIPTION, style=STYLE_PLAIN),
        ],
        [KeyboardButton(text=BTN_FRANCHISE, style=STYLE_PLAIN)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_BTN, style=STYLE_DANGER)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def reply_nav_keyboard(*, telegram_id: int | None = None, is_admin: bool | None = None) -> ReplyKeyboardMarkup:
    """На время пошагового флоу: выход в меню. Шаг назад — inline-кнопкой в сообщении."""
    if is_admin is None:
        is_admin = access.is_staff(telegram_id)
    rows = [[KeyboardButton(text=BTN_HOME, style=STYLE_PLAIN)]]
    if is_admin:
        rows.append([KeyboardButton(text=ADMIN_BTN, style=STYLE_DANGER)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


main_menu_keyboard = reply_main_keyboard
back_keyboard = reply_nav_keyboard


# -------------------------------------------------------------------- inline

def _home_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text=BTN_HOME, callback_data=HOME_CB, style=STYLE_PLAIN)


def inline_home_row() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_home_button()]])


def inline_nav(back_cb: str | None = None) -> InlineKeyboardMarkup:
    """«← Назад» + «🏠 Главное меню» — ряд навигации со всех шагов макета."""
    return InlineKeyboardMarkup(inline_keyboard=[_nav_row(back_cb)])


def _nav_row(back_cb: str | None) -> list[InlineKeyboardButton]:
    row = []
    if back_cb:
        row.append(InlineKeyboardButton(text=BTN_BACK, callback_data=back_cb, style=STYLE_PLAIN))
    row.append(_home_button())
    return row


def inline_error_retry(retry_cb: str) -> InlineKeyboardMarkup:
    """Фреймы «Ошибка» и «Ошибка 2»: изменить текст или выйти в меню."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_EDIT_TEXT, callback_data=retry_cb, style=STYLE_MAIN)],
            [_home_button()],
        ],
    )


def inline_payment_failed(retry_cb: str, back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Фрейм «Уведомление 2» — оплата не прошла."""
    row = []
    if back_cb:
        row.append(InlineKeyboardButton(text=BTN_BACK, callback_data=back_cb, style=STYLE_PLAIN))
    row.append(InlineKeyboardButton(text=BTN_RETRY_PAY, callback_data=retry_cb, style=STYLE_MAIN))
    return InlineKeyboardMarkup(inline_keyboard=[row, [_home_button()]])


# Совместимость со старыми вызовами: раньше на шагах была «Отмена».
inline_cancel_only = inline_home_row
inline_back_cancel = inline_home_row
inline_back_home = inline_home_row
inline_cancel = inline_home_row
inline_publish_cancel = inline_home_row


def inline_main_menu(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    return inline_home_row()


def inline_publish_paywall() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=BTN_SUBSCRIPTION, callback_data="menu:sub", style=STYLE_MAIN)],
            [_home_button()],
        ],
    )


def profile_keyboard() -> InlineKeyboardMarkup:
    """Фрейм «Профиль»."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BTN_TOPUP, callback_data="profile:topup", style=STYLE_MAIN),
                InlineKeyboardButton(text=BTN_MYSUBS, callback_data="profile:mysubs", style=STYLE_PLAIN),
            ],
            [InlineKeyboardButton(text=BTN_INTEGRATION, callback_data="menu:connect", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=BTN_RULES, callback_data="menu:rules", style=STYLE_PLAIN)],
            [_home_button()],
        ],
    )


def cities_keyboard(
    cities,
    callback_prefix: str,
    back_cb: str | None = None,
    *,
    page: int = 0,
    page_prefix: str | None = None,
) -> InlineKeyboardMarkup:
    """Города по два в ряд — как в макете «Шаг 2 · Город».

    Заказчик планирует до 20 городов, поэтому список листается по кругу.
    Пагинация включается только если передан `page_prefix`.
    """
    if page_prefix:
        shown, page, pages = page_slice(cities, page, CITIES_PER_PAGE)
    else:
        shown, pages = list(cities), 1
    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(0, len(shown), 2):
        rows.append([
            InlineKeyboardButton(
                text=city.label,
                callback_data=f"{callback_prefix}{city.id}",
                style=STYLE_PLAIN,
            )
            for city in shown[idx:idx + 2]
        ])
    if page_prefix:
        pager = pager_row(page_prefix, page, pages)
        if pager:
            rows.append(pager)
    rows.append(_nav_row(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def volume_keyboard(options, back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Фрейм «Шаг · Количество публикаций»: объём и цена в одной подписи.

    Формулировку подбирает `posts_volume_button_label` — она держит подпись в
    пределах ёмкости кнопки, чтобы ничего не обрезалось (ТЗ 6.8).
    """
    rows = [
        [
            InlineKeyboardButton(
                text=posts_volume_button_label(key, price),
                callback_data=f"sub:vol:{key}",
                style=STYLE_PLAIN,
            ),
        ]
        for key, _label, price in options
    ]
    rows.append(_nav_row(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def selection_button(text: str, callback_data: str, *, selected: bool) -> InlineKeyboardButton:
    """Пункт множественного выбора — единая механика на все экраны.

    Заказчик сформулировал её так: «при нажатии заливается в синий, ставится
    галочка и появляется зелёная залитая кнопка готово». Невыбранный пункт —
    обычная кнопка с ▫️, выбранный — синяя с ✅.
    """
    return InlineKeyboardButton(
        text=f"{'✅' if selected else '▫️'} {text}",
        callback_data=callback_data,
        style=STYLE_ACTIVE if selected else STYLE_PLAIN,
    )


def done_button(callback_data: str, count: int) -> InlineKeyboardButton:
    """Зелёная «✅ Готово · 2 чата» — появляется, когда что-то выбрано."""
    return InlineKeyboardButton(
        text=f"{BTN_DONE} · {chats_label(count)}",
        callback_data=callback_data,
        style=STYLE_MAIN,
    )


def chats_keyboard(
    chats,
    selected: set[int],
    back_cb: str | None = None,
    *,
    page: int = 0,
) -> InlineKeyboardMarkup:
    """Фрейм «Шаг · Чаты»: отметки ✅ / ▫️, круговая пагинация и «Готово».

    Отмеченные чаты считаются по всему списку, а не по текущей странице, —
    выбор переживает перелистывание.
    """
    shown, page, pages = page_slice(list(chats), page, CHATS_PER_PAGE)
    rows = [
        [selection_button(chat_display_title(chat), f"sub:chat:{chat.id}", selected=chat.id in selected)]
        for chat in shown
    ]
    pager = pager_row("sub:chats:page:", page, pages)
    if pager:
        rows.append(pager)
    if selected:
        rows.append([done_button("sub:chats:done", len(selected))])
    rows.append(_nav_row(back_cb))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscriptions_keyboard(items, *, page: int = 0) -> InlineKeyboardMarkup:
    """Фрейм «Мои подписки»: по кнопке на подписку, список листается по кругу."""
    shown, page, pages = page_slice(list(items), page, SUBS_PER_PAGE)
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"sub:card:{sub_id}", style=STYLE_PLAIN)]
        for sub_id, label in shown
    ]
    pager = pager_row("sub:list:page:", page, pages)
    if pager:
        rows.append(pager)
    rows.append([_home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_card_keyboard(chats) -> InlineKeyboardMarkup:
    """Фрейм «Моя подписка»: чаты подписки ссылками, ниже выход в меню.

    Кнопки «Продлить» и «Удалить» из макета требуют новой механики и в этап
    не входят — вместо них навигация.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=chat_display_title(chat),
                url=f"https://t.me/{(chat.telegram_username or '').lstrip('@')}",
                style=STYLE_PLAIN,
            ),
        ]
        for chat in chats
        if chat.telegram_username
    ]
    rows.append([InlineKeyboardButton(text=BTN_MYSUBS, callback_data="profile:mysubs", style=STYLE_PLAIN)])
    rows.append([_home_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def publication_keyboard(publication_id: int) -> InlineKeyboardMarkup:
    """Фрейм «Уведомление» — действия над опубликованным постом (ТЗ 6.8)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_EDIT_TEXT,
                    callback_data=f"pub:edit:{publication_id}",
                    style=STYLE_MAIN,
                ),
                InlineKeyboardButton(
                    text=BTN_CLOSE_AD,
                    callback_data=f"pub:close:{publication_id}",
                    style=STYLE_DANGER,
                ),
            ],
            [_home_button()],
        ],
    )


def topup_method_keyboard(amount: int, stars: int) -> InlineKeyboardMarkup:
    """Фрейм «Способ пополнения» — порядок кнопок как в макете."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 Банковская карта ({amount:,} ₽)".replace(",", " "),
                    callback_data=f"topup:card:{amount}",
                    style=STYLE_MAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"⭐ Telegram Stars ({stars} ⭐)",
                    callback_data=f"topup:stars:{amount}",
                    style=STYLE_PLAIN,
                ),
            ],
            [_home_button()],
        ],
    )


def payment_choice_keyboard(kind: str, item_id: int, total: int, balance) -> InlineKeyboardMarkup:
    from app.services.wallet import format_rub

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💰 С баланса ({format_rub(balance)} ₽)",
                    callback_data=f"pay:bal:{kind}:{item_id}",
                    style=STYLE_MAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"💳 Оплатить {format_rub(total)} ₽",
                    callback_data=f"pay:card:{kind}:{item_id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [_home_button()],
        ],
    )


def pay_button(url: str, label: str | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label or "💳 Оплатить", url=url, style=STYLE_MAIN)],
            [_home_button()],
        ],
    )


def saved_contacts_keyboard(contacts: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=contact, callback_data=f"pub:contact:{idx}", style=STYLE_PLAIN)]
        for idx, contact in enumerate(contacts[:5])
    ]
    rows.append([InlineKeyboardButton(text="✍️ Другой контакт", callback_data="pub:contact:new", style=STYLE_PLAIN)])
    rows.append(_nav_row("pub:back:text"))
    return InlineKeyboardMarkup(inline_keyboard=rows)
