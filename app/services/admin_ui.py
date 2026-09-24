"""Экран админ-панели: баннер, перерисовка на месте, навигация (ТЗ 6.12, 6.13).

До этапа 2 каждый экран админки уходил новым текстовым сообщением, и переписка
росла на каждый клик. По макету панель перерисовывается на месте, как и
пользовательская часть.

Баннер АДМИНКА при этом стоит только на главном экране панели — кадр
`337:208`, единственный кадр админки с картинкой. Остальные экраны раздела
нарисованы обычными сообщениями, и бот с 20.09.2026 следует этому.

Про кнопку «Назад» ТЗ 6.12 говорит: «на многошаговых сценариях кнопка
"Отмена" заменяется кнопкой "Назад", которая ведёт на один шаг назад, а не
в начало сценария». Стек для этого не нужен — в макете у каждого экрана ровно
один родитель, поэтому шаг передаёт свой `back_cb` явно, как это уже сделано
в пошаговых сценариях пользовательской части.
"""

from __future__ import annotations

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.core.texts import BTN_BACK
from app.keyboards.style import STYLE_PLAIN
from app.services import banners

# Возврат на главный экран панели — подпись из макета.
HOME_CB = "adm:home"
BTN_TO_PANEL = "← В админку"


def back_button(callback_data: str, text: str | None = None) -> InlineKeyboardButton:
    """Кнопка возврата ровно на один шаг назад.

    `None` — это «подпись по умолчанию», а не пустой текст: вызывающие места
    прокидывают сюда необязательный аргумент как есть (`_back(cb, label)` в
    разделе «Города и чаты»), и пока значение по умолчанию задавалось в
    сигнатуре, такой вызов ронял сборку клавиатуры на валидации pydantic —
    экран не открывался вовсе.
    """
    return InlineKeyboardButton(
        text=text or BTN_BACK, callback_data=callback_data, style=STYLE_PLAIN,
    )


def panel_button() -> InlineKeyboardButton:
    return back_button(HOME_CB, BTN_TO_PANEL)


async def show(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    edit: bool | None = None,
    banner: bool = False,
    preview: bool = True,
) -> None:
    """Показать экран панели.

    Правило то же, что и в пользовательской части: клик по кнопке панели
    перерисовывает экран на месте, а команда, кнопка меню или введённый текст
    открывают экран новым сообщением — у покинутого пропадают кнопки.

    Баннер АДМИНКА по умолчанию **не** ставится. Заказчик 20.09.2026: «баннер
    только там стоит, где в фигме, то есть начало раздела». В макете картинка
    есть ровно на одном кадре панели — `337:208`, главный экран; на остальных
    ста с лишним кадрах админки её нет. До этого бот лепил баннер на каждый
    экран, и раздел выглядел лентой одинаковых картинок.

    Переход между экраном с баннером и без него неизбежно уходит новым
    сообщением: фото в текст Telegram не превращает. Внутри админки это ровно
    один переход в каждую сторону — вход с главного экрана и возврат на него;
    переходы между разделами правятся на месте, оба экрана текстовые.

    Покинутый экран при этом НЕ удаляется (заказчик 21.09.2026: «просто новое
    высылать, без удаления») и кнопки на нём остаются, как в админке Hammer:
    бот присылает новое сообщение, а прежний экран остаётся рабочим.
    """
    if isinstance(target, CallbackQuery):
        message = target.message
        edit = True if edit is None else edit
    else:
        message = target
        edit = False if edit is None else edit
    key = banners.ADMIN if banner else banners.PLAIN
    token = None if preview else banners.NO_PREVIEW.set(True)
    try:
        await banners.show_screen(message, key, text, markup, edit=edit, replace=False, keep_markup=True)
    finally:
        if token is not None:
            banners.NO_PREVIEW.reset(token)


async def prompt(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Шаг панели, где администратор вводит текст.

    Правила заказчика (21.09.2026, как в админке Hammer): клик по кнопке только
    обновляет экран на месте, а когда боту нужно прислать текст, он высылает
    новое сообщение. Старый экран при этом не превращается в «мёртвую» копию:
    кнопки на нём остаются (`keep_markup` в `show`).
    Баннера тут нет никогда: шаги ввода в макете нарисованы обычными сообщениями
    (`338:475`, `338:654`, `349:954` и остальные).
    """
    await show(target, text, markup, edit=False)
