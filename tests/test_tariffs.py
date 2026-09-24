"""Справочник тарифов в БД (ТЗ этапа 2, раздел 6.6).

Ключевое требование: «новая стоимость применяется только к новым
публикациям; уже опубликованные и оплаченные объявления не пересчитываются».
Переименование тарифа убрано вслед за макетом — см. `test_rename_is_gone`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.seed import _seed_tariffs
from app.db.session import SessionLocal
from app.ml.categories import CATEGORIES
from app.models.entities import (
    AppSetting,
    Subscription,
    SubscriptionStatus,
    TariffCategory,
)
from app.services import settings_store, tariffs


@pytest.fixture(autouse=True)
async def _seeded():
    """Таблицы чистятся перед каждым тестом — наполняем справочник заново."""
    await _seed_tariffs()
    await tariffs.refresh()
    yield
    await tariffs.refresh()


async def test_seed_fills_every_code_from_the_code_dictionary():
    assert {t.code for t in tariffs.all_tariffs(only_active=False)} | tariffs.SERVICE_CODES == set(CATEGORIES)


async def test_tz_prices_match_the_seed():
    """Все восемь категорий из ТЗ 6.6 совпали с тем, что было в коде."""
    expected = {
        "SHABASHKA": 500,
        "BUY_SELL": 750,
        "RENT": 1000,
        "VACANCY": 1200,
        "REFERRAL": 1500,
        "GOLD": 1800,
        "IP_OOO": 2500,
        "OTHER": 3500,
    }
    for code, price in expected.items():
        assert tariffs.get(code).price_per_chat == price, code


async def test_extra_categories_are_inactive():
    """Двух категорий в справочнике заказчика нет — прячем их из списков."""
    active = {t.code for t in tariffs.all_tariffs()}
    assert "VERIFICATION" not in active
    assert "ONLINE_WORK" not in active
    # Но разрешаются: на них ссылаются старые подписки и предсказания модели.
    assert tariffs.get("VERIFICATION").price_per_chat == 3000


async def test_hidden_categories_are_offered_to_the_admin():
    """Заказчик просит показывать их администратору при ручной проверке."""
    from app.keyboards.admin import admin_log_keyboard

    labels = [
        button.text
        for row in admin_log_keyboard(1).inline_keyboard
        for button in row
    ]
    assert "Верификация" in labels
    assert "Работа онлайн" in labels
    assert "Запрещённый контент" not in labels, "REJECT идёт отдельной красной кнопкой"
    assert "Недостаточно информации" not in labels, "служебный код, не тариф"


async def test_service_codes_are_not_offered_as_tariffs():
    codes = {t.code for t in tariffs.all_tariffs(only_active=False)}
    assert not (codes & tariffs.SERVICE_CODES)


async def test_unknown_code_falls_back_to_other():
    assert tariffs.get("НЕТ ТАКОГО").code == "OTHER"


async def test_set_price():
    await tariffs.set_price("RENT", 1100, by_telegram_id=111)
    assert tariffs.get("RENT").price_per_chat == 1100


async def test_negative_price_rejected():
    with pytest.raises(ValueError):
        await tariffs.set_price("RENT", -1)


def test_rename_is_gone():
    """В макете переименования нет — убрано и из сервиса, и из состояний."""
    from app.states.admin import AdminFlow

    assert not hasattr(tariffs, "rename")
    assert not hasattr(AdminFlow, "tariff_rename")


async def test_price_change_does_not_touch_paid_subscriptions(world):
    """Критерий приёмки 3: изменение тарифа не пересчитывает оплаченное."""
    async with SessionLocal() as session:
        before = (
            await session.scalars(
                select(Subscription).where(Subscription.status == SubscriptionStatus.active),
            )
        ).all()
        snapshot = {s.id: (s.price_per_chat, s.total_price) for s in before}

    await tariffs.set_price("SHABASHKA", 9999, by_telegram_id=111)

    async with SessionLocal() as session:
        after = (
            await session.scalars(
                select(Subscription).where(Subscription.status == SubscriptionStatus.active),
            )
        ).all()
        assert {s.id: (s.price_per_chat, s.total_price) for s in after} == snapshot


async def test_seed_does_not_overwrite_admin_edits():
    """Повторный старт бота не затирает цену, выставленную из панели."""
    await tariffs.set_price("RENT", 1234, by_telegram_id=111)

    await _seed_tariffs()
    await tariffs.refresh()

    assert tariffs.get("RENT").price_per_chat == 1234


async def test_catalog_matches_tz_labels_and_order():
    """Справочник ТЗ 6.6: названия дословно и порядок как в таблице."""
    assert [(t.code, t.label) for t in tariffs.all_tariffs()] == [
        ("SHABASHKA", "Шабашки"),
        ("VACANCY", "Вакансии"),
        ("RENT", "Аренда"),
        ("BUY_SELL", "Купля / продажа"),
        ("GOLD", "Драгоценные металлы"),
        ("IP_OOO", "ИП / ООО"),
        ("REFERRAL", "Рефералки"),
        ("OTHER", "Другое"),
    ]


async def test_catalog_reconcile_runs_once():
    """Сверка выравнивает названия из этапа 1 и больше не повторяется.

    Строки старше сверки эмулируем правкой в обход панели: сбрасываем отметку
    версии и портим название, как это было в боевой базе. Второй прогон — уже
    с отметкой — испорченное название не чинит, потому и «один раз».
    """
    async with SessionLocal() as session:
        row = await session.scalar(select(TariffCategory).where(TariffCategory.code == "SHABASHKA"))
        row.label = "Шабашка / подработка"
        row.sort_order = 99
        marker = await session.scalar(
            select(AppSetting).where(AppSetting.key == settings_store.TARIFF_CATALOG_VERSION),
        )
        assert marker is not None, "первый сид должен был поставить отметку версии"
        await session.delete(marker)
        await session.commit()

    await _seed_tariffs()
    await tariffs.refresh()
    assert tariffs.get("SHABASHKA").label == "Шабашки"
    assert tariffs.all_tariffs()[0].code == "SHABASHKA"

    # Отметка на месте — второй прогон сверку уже не запускает.
    async with SessionLocal() as session:
        row = await session.scalar(select(TariffCategory).where(TariffCategory.code == "SHABASHKA"))
        row.label = "Шабашка / подработка"
        await session.commit()

    await _seed_tariffs()
    await tariffs.refresh()
    assert tariffs.get("SHABASHKA").label == "Шабашка / подработка"


async def test_format_lists_only_active_tariffs():
    text = await tariffs.format_subscription_tariffs()
    assert "Аренда" in text
    assert "Верификация" not in text
    assert "Запрещённый контент" not in text
