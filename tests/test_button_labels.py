"""Длина подписей кнопок пользовательского меню (ТЗ этапа 2, 6.8).

Критерий приёмки 8.8: «текст… приведён в соответствие с макетом Figma»,
причём раздел 6.8 требует, чтобы текст «помещался полностью на всех экранах,
без обрезки». Полноширинная инлайн-кнопка в Telegram на телефоне держит
примерно 24–28 знаков, дальше идёт многоточие.

Своя тонкость: в самом макете подпись длиннее лимита — на шаге 3 объём и цена
собраны в одну строку («До 3 публикаций в день — 3 500 ₽ / чат», 38 знаков).
Заказчик просит держать их вместе, поэтому подпись сжата до «До 3 в день —
3 500 ₽/чат»; в «Моих подписках» по той же причине из кнопки ушла дата.
"""

from __future__ import annotations

import pytest

from app.core.texts import MYSUB_BUTTON, MYSUB_MARK_ACTIVE, MYSUB_MARK_FINISHED
from app.keyboards.main import volume_keyboard
from app.ml.categories import CATEGORIES, CITY_CHOICES
from app.services.pricing import (
    POSTS_VOLUME_LOW,
    POSTS_VOLUME_MID,
    POSTS_VOLUME_UNLIMITED,
    apply_posts_volume,
)
from app.services.textfmt import chats_label

# Практический предел полноширинной кнопки на телефоне.
LIMIT = 28

VOLUMES = (POSTS_VOLUME_LOW, POSTS_VOLUME_MID, POSTS_VOLUME_UNLIMITED)
TARIFF_CODES = [code for code in CATEGORIES if code not in {"INCOMPLETE", "REJECT"}]


def _volume_buttons(base_price: int, category: str):
    from app.services.pricing import POSTS_VOLUME_LABELS

    options = [
        (key, POSTS_VOLUME_LABELS[key], apply_posts_volume(base_price, category, key))
        for key in VOLUMES
    ]
    markup = volume_keyboard(options, "sub:back:city")
    return [button.text for row in markup.inline_keyboard for button in row]


@pytest.mark.parametrize("code", TARIFF_CODES)
def test_volume_buttons_fit_for_every_tariff(code):
    """Все комбинации тарифа и объёма: до этапа 2 не влезала ни одна."""
    for label in _volume_buttons(CATEGORIES[code].price_per_chat, code):
        assert len(label) <= LIMIT, f"{code}: {label!r} — {len(label)} знаков"


def test_volume_button_keeps_volume_and_price_together():
    """Заказчик просит цену в кнопке — и она там, вместе с объёмом."""
    labels = _volume_buttons(3500, "OTHER")
    assert "До 3 в день — 3 500 ₽/чат" in labels
    assert "До 10 в день — 4 000 ₽/чат" in labels
    assert "До 30 в день — 4 500 ₽/чат" in labels


def test_corporate_volume_button_has_no_price():
    """В корпоративном тарифе цена от объёма не зависит — в кнопке один объём."""
    from app.services.pricing import POSTS_VOLUME_LABELS

    options = [(key, POSTS_VOLUME_LABELS[key], None) for key in VOLUMES]
    labels = [button.text for row in volume_keyboard(options).inline_keyboard for button in row]
    assert "До 3 в день" in labels
    for label in labels:
        assert "₽" not in label
        assert len(label) <= LIMIT


@pytest.mark.parametrize("city", [label for _key, label in CITY_CHOICES])
@pytest.mark.parametrize("chats", [1, 2, 5, 12])
@pytest.mark.parametrize("mark", [MYSUB_MARK_ACTIVE, MYSUB_MARK_FINISHED])
def test_subscription_button_fits_for_every_city(city, chats, mark):
    label = MYSUB_BUTTON.format(mark=mark, city=city, chats=chats_label(chats))
    assert len(label) <= LIMIT, f"{label!r} — {len(label)} знаков"
