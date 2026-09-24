"""Обучение модели из панели (ТЗ этапа 2, 6.11).

Раздел «Обучение модели» умеет не только проверять вердикты: заказчик
пополняет выборку примерами руками и файлом, а потом переобучает модель
кнопкой. Тесты держат именно эту часть — однажды её уже вырезали вместе с
кнопками, и классификатор остался без управления из панели.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core import admin_texts as T
from app.core.seed import _seed_tariffs
from app.db.session import SessionLocal
from app.handlers.admin import manage
from app.keyboards.admin import admin_ml_category_keyboard
from app.models.entities import TrainingSample
from app.services import tariffs


@pytest.fixture(autouse=True)
async def _seeded():
    await _seed_tariffs()
    await tariffs.refresh()
    yield
    await tariffs.refresh()


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def test_section_offers_every_training_action():
    """Кнопки раздела: проверка, пополнение выборки и переобучение."""
    callbacks = _callbacks(manage._review_keyboard())
    assert manage.ADD_CB in callbacks, "«Добавить пример» пропала"
    assert manage.FILE_CB in callbacks, "«Загрузить файл» пропала"
    assert manage.RETRAIN_CB in callbacks, "«Переобучить модель» пропала"
    assert f"{manage.LOGS_PAGE_CB}0" in callbacks, "«Логи классификаций» пропали"
    assert f"{manage.PENDING_CB}0" in callbacks
    assert manage.SAMPLES_CB in callbacks


def test_section_buttons_fit_the_row():
    """Подписи не должны обрезаться в кнопке — правка заказчика от 16.09."""
    for label in _labels(manage._review_keyboard()):
        assert len(label) <= 28, f"{label!r} — {len(label)} знаков"


async def test_section_text_shows_sample_counters():
    """На экране видно, сколько примеров уже в модели и сколько добавлено."""
    await manage._save_rows([("Нужен грузчик на склад, оплата ежедневно", "SHABASHKA")], "admin_add")
    text, _markup = await manage._review_screen()
    assert "Добавлено вручную" in text
    assert "<b>1</b>" in text


async def test_manual_sample_lands_in_the_training_set():
    saved, skipped, total = await manage._save_rows(
        [("Сдаётся квартира в центре, длинный текст объявления", "RENT")],
        source="admin_add",
    )
    assert (saved, skipped, total) == (1, 0, 1)

    async with SessionLocal() as session:
        rows = (await session.scalars(select(TrainingSample))).all()
    assert [(r.target_category, r.source) for r in rows] == [("RENT", "admin_add")]


async def test_short_texts_are_skipped_not_saved():
    """Обрывок в выборке только портит модель."""
    saved, skipped, _total = await manage._save_rows(
        [("Нужен грузчик на склад, оплата ежедневно", "SHABASHKA"), ("мало", "VACANCY")],
        source="admin_file",
    )
    assert (saved, skipped) == (1, 1)


async def test_category_keyboard_shows_hidden_categories_too():
    """Размечает выборку администратор — ему нужны все категории (14.09.2026)."""
    await tariffs.ensure_loaded()
    labels = _labels(admin_ml_category_keyboard())
    visible = {t.code for t in tariffs.all_tariffs()}
    hidden = [t.label for t in tariffs.all_tariffs(only_active=False) if t.code not in visible]
    assert hidden, "в справочнике не осталось скрытых категорий — проверять нечего"
    for label in hidden:
        assert label in labels
    assert T.BTN_CATEGORY_REJECT in labels


async def test_category_keyboard_labels_are_not_truncated():
    """Цену в подписи не пишем: с ней длинные названия не влезают."""
    await tariffs.ensure_loaded()
    for label in _labels(admin_ml_category_keyboard()):
        assert "₽" not in label
        assert len(label) <= 28, f"{label!r} — {len(label)} знаков"
