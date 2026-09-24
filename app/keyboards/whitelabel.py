from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core.texts import BTN_HOME, WHITELABEL_URL
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN


def whitelabel_info_keyboard(*, show_apply: bool = True) -> InlineKeyboardMarkup:
    rows = []
    if show_apply:
        rows.append([
            InlineKeyboardButton(
                text="📝 Оставить заявку",
                callback_data="wl:apply",
                style=STYLE_MAIN,
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="📢 Канал HLSWX", url=WHITELABEL_URL, style=STYLE_PLAIN),
    ])
    rows.append([
        InlineKeyboardButton(text=BTN_HOME, callback_data="menu:home", style=STYLE_PLAIN),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
