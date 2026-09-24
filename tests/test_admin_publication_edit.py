"""Правка опубликованного объявления из админки (ТЗ этапа 2, 6.9).

ТЗ требует у опубликованной публикации действие «Изменить» — до этого в
карточке не было вообще никаких кнопок, а механика правки была доступна
только автору объявления.

Два места, где это легко сломать, и оба закрыты тестами ниже:
— callback правки не должен попадать в обработчик действий, иначе вместо
  правки задача снимется с очереди;
— кнопка бессмысленна у задач без публикации (разовые и пакетные посты).
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.session import SessionLocal
from app.handlers.admin.publications import (
    ACTION_CB,
    CARD_CB,
    EDIT_CB,
    _card,
)
from app.models.entities import Publication, PublishJob
from app.services.publication_status import apply_publication_edit
from app.services.publish import process_publish_queue


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def _callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


async def _published_job(bot, enqueue) -> PublishJob:
    await enqueue("sub1")
    await process_publish_queue(bot)
    async with SessionLocal() as session:
        return await session.scalar(
            select(PublishJob).where(PublishJob.status == "published").order_by(PublishJob.id),
        )


async def test_edit_callback_is_not_swallowed_by_the_action_handler():
    """Обработчик действий отменяет всё, что не узнал, — правка должна жить вне него."""
    assert not f"{EDIT_CB}42".startswith(ACTION_CB)


async def test_published_card_offers_edit(bot, world, enqueue):
    job = await _published_job(bot, enqueue)
    async with SessionLocal() as session:
        _, markup = await _card(session, job)

    assert "✏️ Изменить" in _labels(markup)
    assert f"{EDIT_CB}{job.id}" in _callbacks(markup)


async def test_card_without_publication_has_no_edit(bot, world, enqueue):
    """У задачи без публикации править нечего: `publication_id` пустой."""
    job = await _published_job(bot, enqueue)
    async with SessionLocal() as session:
        job.publication_id = None
        session.add(job)
        await session.commit()
        _, markup = await _card(session, job)

    assert "✏️ Изменить" not in _labels(markup)


async def test_queued_card_keeps_its_own_actions(bot, world, enqueue):
    """Правка добавлена только опубликованным — очередь не задета."""
    await enqueue("sub1")
    async with SessionLocal() as session:
        job = await session.scalar(select(PublishJob).order_by(PublishJob.id))
        _, markup = await _card(session, job)

    labels = _labels(markup)
    assert "⏭ Опубликовать сейчас" in labels
    assert "❌ Отменить" in labels
    assert "✏️ Изменить" not in labels


async def test_admin_edit_replaces_text_everywhere(bot, world, enqueue):
    """Правка администратора не требует владения публикацией."""
    job = await _published_job(bot, enqueue)
    publication_id = job.publication_id

    updated, failed, pending = await apply_publication_edit(bot, publication_id, "поправленный текст")

    assert updated >= 1
    assert failed == 0
    async with SessionLocal() as session:
        publication = await session.scalar(
            select(Publication).where(Publication.id == publication_id),
        )
        jobs = list(
            (await session.scalars(select(PublishJob).where(PublishJob.publication_id == publication_id))).all(),
        )
    assert publication.text == "поправленный текст"
    assert {j.text for j in jobs} == {"поправленный текст"}
    assert pending == sum(1 for j in jobs if j.status == "queued")


async def test_card_back_button_returns_to_the_card_not_the_list(bot, world, enqueue):
    """С экрана ввода текста «Назад» ведёт на шаг назад — в карточку (ТЗ 6.12)."""
    job = await _published_job(bot, enqueue)
    assert f"{CARD_CB}{job.id}".startswith(CARD_CB)
