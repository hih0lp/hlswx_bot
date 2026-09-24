"""Подписи записей в списках панели: три строки и ни одной обрезанной.

`test_admin_button_widths.py` разбирает исходники и видит только литералы и
константы `T.BTN_*`. Подписи записей собираются f-строками из данных, и та
проверка их не замечает — дыру нашли при сверке с макетом 18.09.2026. Здесь
подписи строятся по-настоящему, на нарочно длинных данных.

Кадры: `341:1112` и `340:751` (очередь и публикации), `338:725` (белый
список), `349:1025` (пользователи). Во всех подпись занимает полный ряд,
то есть предел — 28 знакомест на строку, как в `test_button_labels.py`.

Многострочность — не общее правило: в белом списке подпись по кадру 338:725
однострочная, и `_check` там не применяется.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.session import SessionLocal
from app.handlers.admin.publications import job_label
from app.handlers.admin.users import user_label
from app.handlers.admin.whitelist import entry_label
from app.models.entities import Chat, PublishJob, User, WhitelistEntry
from tests.test_admin_button_widths import _line_width

LIMIT = 28


def _widest(label: str) -> tuple[str, int]:
    worst = max(label.split("\n"), key=_line_width)
    return worst, _line_width(worst)


def _check(label: str) -> None:
    assert label.count("\n") >= 1, f"подпись должна быть многострочной: {label!r}"
    line, width = _widest(label)
    assert width <= LIMIT, f"{line!r} — {width} знакомест, влезает {LIMIT}"


async def test_queue_job_label_fits(world, bot):
    """Запись очереди: «#id · автор», «город · чат», «когда»."""
    async with SessionLocal() as session:
        chat = await session.scalar(select(Chat).where(Chat.id == world.chat_ids[0]))
        job = PublishJob(
            chat_id=chat.id,
            author_user_id=world.user_ids["sub1"],
            status="queued",
            scheduled_at=datetime.now(UTC) + timedelta(minutes=2),
            text="объявление",
            contact="@irina_sh75ui",
        )
        session.add(job)
        await session.commit()
        label = await job_label(session, job)
    _check(label)
    # В макете строка длиннее («Следующее сообщение: через 2 мин»), но в кнопку
    # это не влезает — в боте осталось «Через 2 мин», макет правится плагином.
    assert "Через 2 мин" in label


@pytest.mark.parametrize("status", ["published", "failed"])
async def test_job_label_fits_for_every_status(world, bot, status):
    async with SessionLocal() as session:
        job = PublishJob(
            chat_id=world.chat_ids[0],
            author_user_id=world.user_ids["sub1"],
            status=status,
            published_at=datetime.now(UTC),
            failed_at=datetime.now(UTC),
            error="chat_not_found",
            text="объявление",
            contact="@irina_sh75ui",
        )
        session.add(job)
        await session.commit()
        label = await job_label(session, job)
    _check(label)


async def test_whitelist_entry_label_fits(world, bot):
    """Запись белого списка: по кадру 338:725 — одна строка «#id · @username».

    Охват и срок стояли тут до 20.09.2026 по кадру 349:580 — второму рисунку
    того же экрана. Заказчик указал на 338:725, и проверка теперь следит за
    обратным: чтобы подпись не разрослась снова.
    """
    entry = WhitelistEntry(
        id=12,
        telegram_id=1904406102,
        username="irina_sh75ui",
        city_keys='["msk", "spb", "nn", "smr", "yar"]',
        chat_ids="[1, 2, 3, 4, 5]",
        expires_at=datetime.now(UTC) + timedelta(days=2),
    )
    label = entry_label(entry)
    assert label == "#12 · @irina_sh75ui"
    assert _line_width(label) <= LIMIT


async def test_whitelist_entry_label_without_username(world, bot):
    """Без username подписью остаётся Telegram ID — иначе запись безымянная."""
    entry = WhitelistEntry(id=1, telegram_id=1904406102)
    label = entry_label(entry)
    assert label == "#1 · 1904406102"
    assert _line_width(label) <= LIMIT


async def test_users_label_fits(world, bot):
    user = User(id=7, telegram_id=1904406102, username="irina_sh75ui", full_name="Ирина")
    _check(user_label(user, active=True, subscriptions=2))
    _check(user_label(user, active=False, subscriptions=0))
