"""Справочник городов и групп: перечень заказчика от 14.09.2026 (ТЗ этапа 2, 6.5)."""

from __future__ import annotations

from sqlalchemy import select

from app.core.seed import CHAT_SEED, _seed_places
from app.db.session import SessionLocal
from app.ml.categories import CITY_CHOICES
from app.models.entities import Chat, City


async def _chats() -> dict[str, Chat]:
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Chat))).all()
        return {row.telegram_username: row for row in rows}


async def test_seed_creates_full_catalog():
    await _seed_places()

    async with SessionLocal() as session:
        cities = (await session.scalars(select(City))).all()
        chats = (await session.scalars(select(Chat))).all()

    assert len(cities) == len(CITY_CHOICES) == 10
    assert {c.key for c in cities} >= {"ekb", "kzn", "kld", "chel"}
    assert len(chats) == len(CHAT_SEED) == 20
    assert all(chat.active for chat in chats)
    assert all(chat.telegram_chat_id for chat in chats)


async def test_seed_is_idempotent():
    await _seed_places()
    await _seed_places()

    async with SessionLocal() as session:
        assert len(list((await session.scalars(select(Chat))).all())) == len(CHAT_SEED)


async def test_renamed_group_keeps_its_row():
    """Группу переименовали, telegram id прежний — строка та же, а не вторая."""
    async with SessionLocal() as session:
        city = City(key="msk", label="Москва")
        session.add(city)
        await session.flush()
        session.add(
            Chat(
                city_id=city.id,
                title="Работа Москва 2",
                telegram_username="Rabota77Hammer",
                telegram_chat_id=-1002321186753,
                topic="Работа",
                network="hammer",
            ),
        )
        await session.commit()
        old_id = (
            await session.scalar(
                select(Chat).where(Chat.telegram_username == "Rabota77Hammer"),
            )
        ).id

    await _seed_places()

    chats = await _chats()
    assert "Rabota77Hammer" not in chats
    assert chats["HalturamskHammer"].id == old_id
    assert chats["HalturamskHammer"].title == "Шабашка Москва"


async def test_group_with_new_id_updates_in_place():
    """У Новосибирска сменился сам telegram id — строку узнаём по прежнему username."""
    async with SessionLocal() as session:
        city = City(key="nsk", label="Новосибирск")
        session.add(city)
        await session.flush()
        session.add(
            Chat(
                city_id=city.id,
                title="Работа Новосибирск",
                telegram_username="Rabota_Nsk_W",
                telegram_chat_id=-1003921188790,
                topic="Работа",
                network="w",
            ),
        )
        await session.commit()
        old_id = (
            await session.scalar(
                select(Chat).where(Chat.telegram_username == "Rabota_Nsk_W"),
            )
        ).id

    await _seed_places()

    chats = await _chats()
    assert "Rabota_Nsk_W" not in chats
    row = chats["Podrabotkanskw"]
    assert row.id == old_id, "подписки привязаны к Chat.id — строка должна остаться той же"
    assert row.telegram_chat_id == -1001692649674


async def test_groups_outside_the_list_are_disabled():
    """Три московские группы, которых в перечне нет, отключаются, а не удаляются."""
    async with SessionLocal() as session:
        city = City(key="msk", label="Москва")
        session.add(city)
        await session.flush()
        for username, tg_id, title in [
            ("Job_Moscow_Today", -1003873556203, "Job Moscow Today"),
            ("mos_vacancy", -1003795137515, "Mos Vacancy"),
            ("Vacancies_Moskow", -1003834494468, "Vacancies Moscow"),
        ]:
            session.add(
                Chat(
                    city_id=city.id,
                    title=title,
                    telegram_username=username,
                    telegram_chat_id=tg_id,
                    topic="Партнёр",
                    network="package",
                ),
            )
        await session.commit()

    await _seed_places()

    chats = await _chats()
    for username in ("Job_Moscow_Today", "mos_vacancy", "Vacancies_Moskow"):
        assert username in chats, "строка нужна: на неё ссылается журнал публикаций"
        assert chats[username].active is False


async def test_chat_added_from_panel_survives_reconcile():
    """Чат, заведённый администратором, сверка не переписывает — только отключает по перечню."""
    async with SessionLocal() as session:
        city = City(key="msk", label="Москва")
        session.add(city)
        await session.flush()
        session.add(
            Chat(
                city_id=city.id,
                title="Свой чат",
                telegram_username="own_chat_w",
                telegram_chat_id=-100_500,
                topic="Работа",
                network="package",
            ),
        )
        await session.commit()

    await _seed_places()

    chats = await _chats()
    assert chats["own_chat_w"].title == "Свой чат"
    assert chats["own_chat_w"].active is False
