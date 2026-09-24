"""Экраны раздела «Тарифы» по кадрам макета (ТЗ этапа 2, 6.6).

Кадры 375:454 → 375:510 → 375:618 → 375:674: список стоимостей стоит в тексте
экрана, кнопка на нём одна, за ней выбор категории по две в ряд и ввод новой
стоимости. Переименования в макете нет.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core import admin_texts as T
from app.core.seed import _seed_tariffs
from app.handlers.admin import tariffs as section
from app.ml.categories import CATEGORY_ICONS, category_icon
from app.services import tariffs

SOURCE = Path(__file__).resolve().parents[1] / "app" / "handlers" / "admin" / "tariffs.py"


@pytest.fixture(autouse=True)
async def _seeded():
    await _seed_tariffs()
    await tariffs.refresh()
    yield
    await tariffs.refresh()


async def test_list_of_prices_lives_in_the_screen_text():
    """Кадр 375:454: восемь строк со значком, названием и ценой."""
    lines = "\n".join(
        section._tariff_label(t.code, t.label, t.price_per_chat)
        for t in tariffs.all_tariffs()
    )
    text = T.TARIFFS.format(lines=lines)

    assert "💼 Шабашки · 500 ₽" in text
    assert "📌 Другое · 3 500 ₽" in text
    assert text.count("\n") >= 8


async def test_every_visible_category_has_an_icon_from_the_mockup():
    for tariff in tariffs.all_tariffs():
        assert tariff.code in CATEGORY_ICONS, tariff.code
        assert category_icon(tariff.code) != "🏷"


async def test_price_screens_show_the_category():
    ask = T.TARIFF_PRICE_ASK.format(label="💼 Шабашки", price="500")
    assert "💼 Шабашки" in ask
    assert "Введите новую стоимость" in ask

    saved = T.TARIFF_SAVED_PRICE.format(label="💼 Шабашки", old="500", new="750")
    assert "500 ₽ → 750 ₽" in saved
    assert "не пересчитываются" in saved


def test_categories_are_picked_two_per_row():
    assert section.PICK_PER_ROW == 2


def test_rename_left_the_section():
    """В макете кнопки переименования нет — и в разделе её быть не должно."""
    source = SOURCE.read_text(encoding="utf-8")
    assert "rename" not in source.lower()
    assert not hasattr(T, "BTN_TARIFF_RENAME")
    assert not hasattr(T, "TARIFF_SAVED_NAME")


def test_section_has_a_single_action_button():
    """Кадр 375:454: карточки категории нет, действие одно."""
    source = SOURCE.read_text(encoding="utf-8")
    buttons = set(re.findall(r"text=T\.(BTN_\w+)", source))
    assert buttons == {"BTN_TARIFF_PRICE"}
