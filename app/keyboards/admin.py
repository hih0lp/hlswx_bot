"""Клавиатуры админ-панели.

Палитра — та же, что в пользовательской части (`keyboards.style`): пункт меню
обычный, главное действие экрана зелёное, необратимое красное. Стрелка возврата
пишется «←», как в макете и в пользовательских экранах.
"""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.core import admin_texts as T
from app.core.texts import BTN_BACK
from app.keyboards.style import STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.services import tariffs


def admin_wlreq_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏳ Ожидают", callback_data="adm:wlreq:list:pending", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text="📋 Все заявки", callback_data="adm:wlreq:list:all", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_BACK_TO_PARTNERS, callback_data="adm:wlbl", style=STYLE_PLAIN)],
        ],
    )


def admin_wlreq_view_keyboard(app_id: int, *, can_partner: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"adm:wlreq:approve:{app_id}", style=STYLE_MAIN),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm:wlreq:reject:{app_id}", style=STYLE_DANGER),
        ],
    ]
    if can_partner:
        rows.append([
            InlineKeyboardButton(
                text="🤖 Создать партнёра",
                callback_data=f"adm:wlreq:partner:{app_id}",
                style=STYLE_PLAIN,
            ),
        ])
    rows.append([InlineKeyboardButton(text=T.BTN_BACK_TO_WLREQ, callback_data="adm:wlreq", style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Порядок категорий в кнопках выбора — кадр макета 349:381. Он отличается от
# порядка в «Тарифах» (там «Аренда» идёт раньше «Купли / продажи», а «Другое»
# последнее), поэтому задан отдельно; неизвестные коды уходят в конец.
_PICKER_ORDER = (
    "SHABASHKA", "VACANCY", "BUY_SELL", "RENT", "OTHER",
    "GOLD", "IP_OOO", "REFERRAL", "VERIFICATION", "ONLINE_WORK",
)


def _picker_tariffs():
    """Все категории (и скрытые) в порядке макета для выбора вручную."""
    rank = {code: i for i, code in enumerate(_PICKER_ORDER)}
    return sorted(
        tariffs.all_tariffs(only_active=False),
        key=lambda t: rank.get(t.code, len(rank)),
    )


def admin_ml_category_keyboard(back_cb: str = "adm:ml") -> InlineKeyboardMarkup:
    """Категория для примера, добавленного в выборку руками (ТЗ 6.11).

    Тот же список, что и при проверке вердикта в `admin_log_keyboard`, включая
    скрытые категории: размечает выборку администратор, и ему нужны все.
    Цену в подписи не пишем — с ней длинные названия обрезаются в кнопке
    (правка заказчика от 16.09 про «Пополнить баланс»).
    """
    rows = [
        [
            InlineKeyboardButton(
                text=tariff.label,
                callback_data=f"adm:ml:cat:{tariff.code}",
                style=STYLE_PLAIN,
            ),
        ]
        for tariff in _picker_tariffs()
    ]
    rows.append([
        InlineKeyboardButton(
            text=T.BTN_CATEGORY_REJECT,
            callback_data="adm:ml:cat:REJECT",
            style=STYLE_DANGER,
        ),
    ])
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data=back_cb, style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_log_keyboard(log_id: int, *, back_cb: str = "adm:ml") -> InlineKeyboardMarkup:
    """Выбор категории вручную — кадр макета 349:381.

    Названия берутся из справочника тарифов: до этапа 2 здесь лежал
    захардкоженный список кодов, и администратор видел в кнопках
    «🏷 SHABASHKA» вместо названия категории.

    Та же клавиатура уходит админам в уведомлении «Тариф на проверке»
    (`app/handlers/subscription.py`), поэтому возврат настраивается: из
    уведомления — в раздел проверки, из карточки — в саму карточку.

    Единственный список, где показываются скрытые категории («Верификация» и
    «Работа онлайн»): по решению заказчика от 14.09.2026 они видны только
    администратору при ручной проверке. У пользователя и в разделе «Тарифы»
    их по-прежнему нет — там `all_tariffs()` без аргумента.
    """
    rows = [
        [
            InlineKeyboardButton(
                text=tariff.label,
                callback_data=f"adm:fix:{log_id}:{tariff.code}",
                style=STYLE_PLAIN,
            ),
        ]
        for tariff in _picker_tariffs()
    ]
    rows.append([
        InlineKeyboardButton(
            text=T.BTN_CATEGORY_REJECT,
            callback_data=f"adm:fix:{log_id}:REJECT",
            style=STYLE_DANGER,
        ),
    ])
    rows.append([InlineKeyboardButton(text=BTN_BACK, callback_data=back_cb, style=STYLE_PLAIN)])
    return InlineKeyboardMarkup(inline_keyboard=rows)
