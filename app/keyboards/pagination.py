"""Круговая пагинация списков (ТЗ этапа 2, п. 6.8).

Заказчик в комментарии к макету: «городов всего будет до 20, будет удобнее
сделать кнопку найти город как сейчас или круговой зацикленный вариант
с пагинацией». Круговая — значит «вперёд» с последней страницы ведёт на
первую, а «назад» с первой — на последнюю; упоров у списка нет, поэтому
и отдельных проверок границ в вызывающем коде тоже нет: номер страницы
нормализуется по модулю.

Ряд пагинации рисуется только когда страниц больше одной — на коротком
списке лишних кнопок не появляется.
"""

from __future__ import annotations

from typing import Sequence, TypeVar

from aiogram.types import InlineKeyboardButton

from app.keyboards.style import STYLE_PLAIN

T = TypeVar("T")

# Счётчик страниц кликабелен только технически: Telegram требует у кнопки
# callback_data, поэтому отвечаем на него пустым answer().
NOOP_CB = "noop"

BTN_PREV = "⬅️"
BTN_NEXT = "➡️"


def page_count(total: int, per_page: int) -> int:
    """Сколько страниц у списка из `total` элементов. У пустого списка — одна."""
    if per_page <= 0:
        return 1
    return max(1, -(-total // per_page))


def page_slice(items: Sequence[T], page: int, per_page: int) -> tuple[list[T], int, int]:
    """Элементы страницы, нормализованный номер страницы и число страниц.

    Номер приводится по модулю, поэтому `-1` — это последняя страница,
    а `pages` — снова первая. Нумерация страниц с нуля.
    """
    pages = page_count(len(items), per_page)
    page = page % pages
    start = page * per_page
    return list(items[start:start + per_page]), page, pages


def pager_row(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    """Ряд «⬅️ · 2 / 5 · ➡️» (заказчик 21.09.2026, во всём боте).

    Пустой список, когда страница всего одна.

    `prefix` дописывается номером целевой страницы, например `sub:chats:page:`.
    """
    if pages <= 1:
        return []
    return [
        InlineKeyboardButton(
            text=BTN_PREV,
            callback_data=f"{prefix}{(page - 1) % pages}",
            style=STYLE_PLAIN,
        ),
        InlineKeyboardButton(
            text=f"{page + 1} / {pages}",
            callback_data=NOOP_CB,
            style=STYLE_PLAIN,
        ),
        InlineKeyboardButton(
            text=BTN_NEXT,
            callback_data=f"{prefix}{(page + 1) % pages}",
            style=STYLE_PLAIN,
        ),
    ]
