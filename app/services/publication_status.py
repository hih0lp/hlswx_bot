"""Статус публикации и уведомления пользователю (ТЗ 6.8).

Пост пользователя = одна Publication и по задаче на каждую адресованную группу.
Пока пост вышел не во всех группах, публикация считается частичной.
"""

from __future__ import annotations

import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.texts import (
    NOTIFY_CLOSED_MARK,
    NOTIFY_FAILED,
    NOTIFY_PUBLISHED_HEADER,
    NOTIFY_QUEUED,
)
from app.db.session import SessionLocal
from app.keyboards.main import publication_keyboard
from app.models.entities import Chat, Publication, PublishJob, User
from app.services.chat_network import chat_display_title
from app.services.publish_errors import STATUS_UNCONFIRMED
from app.services.publish_log import log_event
from app.services.textfmt import chats_label

logger = logging.getLogger("publication_status")

STATE_QUEUED = "queued"
STATE_PARTIAL = "partial"
STATE_PUBLISHED = "published"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

_PENDING_STATUSES = ("queued",)
_DEAD_STATUSES = ("failed", "expired", "cancelled")
# Задача с оборванным ответом: пост почти наверняка в группе, поэтому для
# пользователя она ничем не отличается от опубликованной. Разбирается с ней
# администратор — ему уходит отдельное уведомление.
_LANDED_STATUSES = ("published", STATUS_UNCONFIRMED)


async def _chat_titles(session: AsyncSession, jobs: list[PublishJob]) -> dict[int, str]:
    titles: dict[int, str] = {}
    for job in jobs:
        if job.chat_id in titles:
            continue
        chat = await session.scalar(select(Chat).where(Chat.id == job.chat_id))
        titles[job.chat_id] = chat_display_title(chat) if chat else f"чат #{job.chat_id}"
    return titles


def _compute_state(jobs: list[PublishJob]) -> str:
    if not jobs:
        return STATE_CANCELLED
    published = [j for j in jobs if j.status in _LANDED_STATUSES]
    pending = [j for j in jobs if j.status in _PENDING_STATUSES]
    if pending and not published:
        return STATE_QUEUED
    if pending and published:
        return STATE_PARTIAL
    if published:
        return STATE_PUBLISHED
    return STATE_FAILED


def _render_notification(
    publication: Publication,
    jobs: list[PublishJob],
    titles: dict[int, str],
    state: str,
) -> str:
    """Три состояния из макета: «в очереди», «опубликовано», «опубликовано частично».

    Частичная публикация в макете показана тем же экраном, что и полная, —
    отличается только список чатов с отметками ✅ / ⏳.
    """
    body = (publication.text or "").strip()

    if state == STATE_QUEUED:
        lines = [NOTIFY_QUEUED]
    elif state in (STATE_PUBLISHED, STATE_PARTIAL):
        # В макете («Уведомление») текст объявления лежит в серой цитате.
        # Экранируем: текст пользовательский, а сообщение уходит в HTML-режиме —
        # одна «<» в объявлении иначе ломала бы само уведомление.
        lines = [NOTIFY_PUBLISHED_HEADER, ""]
        if body:
            lines.append(f"<blockquote>{html.escape(body, quote=False)}</blockquote>")
        if len(jobs) > 1:
            marks = {status: "✅" for status in _LANDED_STATUSES}
            marks.update({status: "⏳" for status in _PENDING_STATUSES})
            marks.update({status: "⚠️" for status in _DEAD_STATUSES})
            lines.append("")
            lines.append(f"<b>💬 {chats_label(len(jobs))}</b>")
            lines.append("")
            for job in jobs:
                lines.append(f"{marks.get(job.status, '⏳')} {titles[job.chat_id]}")
    else:
        dead = [j for j in jobs if j.status in _DEAD_STATUSES]
        timed_out = bool(dead) and all(j.status == "expired" for j in dead)
        reason = (
            "Истёк срок ожидания в очереди."
            if timed_out
            else "Не удалось опубликовать ни в одной группе."
        )
        lines = [NOTIFY_FAILED.format(reason=reason)]

    if publication.closed:
        lines.append("")
        lines.append(NOTIFY_CLOSED_MARK)
    return "\n".join(lines)


async def refresh_publication_status(bot: Bot, publication_id: int) -> str | None:
    """Пересчитать статус публикации и уведомить пользователя при смене состояния."""
    async with SessionLocal() as session:
        publication = await session.scalar(select(Publication).where(Publication.id == publication_id))
        if not publication:
            return None
        jobs = list(
            (
                await session.scalars(
                    select(PublishJob)
                    .where(PublishJob.publication_id == publication_id)
                    .order_by(PublishJob.id),
                )
            ).all(),
        )
        state = _compute_state(jobs)
        if publication.closed and state != STATE_PUBLISHED:
            state = STATE_CANCELLED
        publication.status = state
        if state in (STATE_PUBLISHED, STATE_FAILED) and not publication.completed_at:
            from datetime import UTC, datetime

            publication.completed_at = datetime.now(UTC)

        # Закрытие объявления меняет карточку, но не состояние очереди: у поста,
        # который уже везде вышел, state остаётся published. Поэтому в отметку
        # «о чём уже уведомили» закрытие входит отдельным признаком.
        notify_key = f"{state}:closed" if publication.closed else state
        if notify_key == publication.notified_state:
            await session.commit()
            return state

        user = await session.scalar(select(User).where(User.id == publication.user_id))
        if not user:
            await session.commit()
            return state

        titles = await _chat_titles(session, jobs)
        text = _render_notification(publication, jobs, titles, state)
        # В макете кнопки «Изменить текст» и «Закрыть объявление» есть и на
        # частично опубликованном посте — там уже есть что править.
        keyboard = (
            publication_keyboard(publication_id)
            if state in (STATE_PUBLISHED, STATE_PARTIAL) and not publication.closed
            else None
        )

        sent_id = await _send_or_edit(bot, user.telegram_id, publication, text, keyboard)
        if sent_id:
            publication.notify_message_id = sent_id
        publication.notified_state = notify_key
        await log_event(
            session,
            "notified",
            publication_id=publication_id,
            detail=f"state={state}",
        )
        await session.commit()
        return state


async def _send_or_edit(
    bot: Bot,
    telegram_id: int,
    publication: Publication,
    text: str,
    keyboard: InlineKeyboardMarkup | None,
) -> int | None:
    """Одно уведомление на публикацию: обновляем его по мере разбора очереди."""
    if publication.notify_message_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=telegram_id,
                message_id=publication.notify_message_id,
                reply_markup=keyboard,
            )
            return publication.notify_message_id
        except Exception:
            logger.debug("Notify edit failed, sending new message", exc_info=True)
    try:
        message = await bot.send_message(telegram_id, text, reply_markup=keyboard)
    except Exception:
        logger.exception("Failed to notify user %s about publication %s", telegram_id, publication.id)
        return None

    # Показанный экран больше не последнее сообщение в чате — обновлять его
    # на месте нельзя, следующий раздел уйдёт новым сообщением.
    from app.services.banners import forget_screen

    forget_screen(telegram_id)
    return message.message_id


async def apply_publication_edit(bot: Bot, publication_id: int, new_text: str) -> tuple[int, int, int]:
    """Правка текста во всех группах, где пост уже вышел.

    Возвращает (обновлено, не удалось, ждут в очереди).
    """
    from app.services.direct_publish import DELIVERY_DIRECT, direct_edit
    from app.services.hammer_relay import relay_edit

    updated = failed = 0
    async with SessionLocal() as session:
        publication = await session.scalar(select(Publication).where(Publication.id == publication_id))
        if not publication:
            return 0, 0, 0
        publication.text = new_text
        jobs = list(
            (await session.scalars(select(PublishJob).where(PublishJob.publication_id == publication_id))).all(),
        )
        pending = 0
        for job in jobs:
            job.text = new_text
            if job.status == "queued":
                pending += 1
                continue
            if job.status != "published" or not job.message_id:
                continue
            chat = await session.scalar(select(Chat).where(Chat.id == job.chat_id))
            if not chat or not chat.telegram_chat_id:
                failed += 1
                continue
            if job.delivery == DELIVERY_DIRECT:
                ok, error = await direct_edit(
                    bot,
                    telegram_chat_id=int(chat.telegram_chat_id),
                    message_id=job.message_id,
                    text=new_text,
                    contact=job.contact,
                    has_photo=bool(job.photo_url),
                )
            else:
                ok, error = await relay_edit(
                    telegram_chat_id=int(chat.telegram_chat_id),
                    message_id=job.message_id,
                    text=new_text,
                    contact=job.contact,
                    network=chat.network or "hammer",
                )
            if ok:
                updated += 1
            else:
                failed += 1
                logger.warning("Edit failed job=%s: %s", job.id, error)
        await log_event(
            session,
            "edited",
            publication_id=publication_id,
            detail=f"updated={updated} failed={failed} pending={pending}",
        )
        await session.commit()
    return updated, failed, pending


async def apply_publication_close(bot: Bot, publication_id: int) -> tuple[int, int, int]:
    """Зачеркнуть объявление везде, где вышло, и снять оставшиеся задачи.

    Возвращает (закрыто, не удалось, отменено в очереди).
    """
    from app.services.direct_publish import DELIVERY_DIRECT, direct_close
    from app.services.hammer_relay import relay_close

    closed = failed = cancelled = 0
    async with SessionLocal() as session:
        publication = await session.scalar(select(Publication).where(Publication.id == publication_id))
        if not publication:
            return 0, 0, 0
        publication.closed = True
        jobs = list(
            (await session.scalars(select(PublishJob).where(PublishJob.publication_id == publication_id))).all(),
        )
        for job in jobs:
            if job.status == "queued":
                job.status = "cancelled"
                job.error = "closed_by_user"
                cancelled += 1
                continue
            if job.status != "published" or not job.message_id:
                continue
            chat = await session.scalar(select(Chat).where(Chat.id == job.chat_id))
            if not chat or not chat.telegram_chat_id:
                failed += 1
                continue
            if job.delivery == DELIVERY_DIRECT:
                ok, error = await direct_close(
                    bot,
                    telegram_chat_id=int(chat.telegram_chat_id),
                    message_id=job.message_id,
                    text=job.text,
                    contact=job.contact,
                    has_photo=bool(job.photo_url),
                )
            else:
                ok, error = await relay_close(
                    telegram_chat_id=int(chat.telegram_chat_id),
                    message_id=job.message_id,
                    text=job.text,
                    contact=job.contact,
                    network=chat.network or "hammer",
                )
            if ok:
                closed += 1
            else:
                failed += 1
                logger.warning("Close failed job=%s: %s", job.id, error)
        await log_event(
            session,
            "closed",
            publication_id=publication_id,
            detail=f"closed={closed} failed={failed} cancelled={cancelled}",
        )
        await session.commit()
    return closed, failed, cancelled
