"""Кнопки «Редактировать» и «Закрыть» под опубликованным постом (ТЗ 6.8)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from app.db.session import SessionLocal
from app.keyboards.main import inline_home_row
from app.models.entities import Publication, User
from app.services.publication_status import (
    apply_publication_close,
    apply_publication_edit,
    refresh_publication_status,
)
from app.services.users import get_or_create_user
from app.states.flows import PublicationEditFlow

logger = logging.getLogger("publication_actions")

publication_actions_router = Router()


async def _owned_publication(telegram_id: int, publication_id: int) -> Publication | None:
    async with SessionLocal() as session:
        publication = await session.scalar(select(Publication).where(Publication.id == publication_id))
        if not publication:
            return None
        user = await session.scalar(select(User).where(User.id == publication.user_id))
        if not user or user.telegram_id != telegram_id:
            return None
        return publication


@publication_actions_router.callback_query(F.data.startswith("pub:edit:"))
async def publication_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    publication_id = int(callback.data.split(":")[2])
    publication = await _owned_publication(callback.from_user.id, publication_id)
    if not publication:
        await callback.answer("Публикация не найдена", show_alert=True)
        return
    if publication.closed:
        await callback.answer("Объявление уже закрыто", show_alert=True)
        return

    await callback.answer()
    await state.set_state(PublicationEditFlow.waiting_text)
    await state.update_data(edit_publication_id=publication_id)
    # Отдельного экрана правки в макете нет — формулировка по его стилю.
    await callback.message.answer(
        "<b>✏️ Изменить текст</b>\n\n"
        "Отправьте новый текст объявления одним сообщением.\n"
        "Он заменит объявление во всех чатах, где оно уже опубликовано.",
    )


@publication_actions_router.message(PublicationEditFlow.waiting_text)
async def publication_edit_apply(message: Message, state: FSMContext) -> None:
    from app.services.menu_nav import dispatch_menu_button

    if await dispatch_menu_button(message, state):
        return

    data = await state.get_data()
    publication_id = data.get("edit_publication_id")
    new_text = (message.text or "").strip()
    if not publication_id:
        await state.clear()
        return
    if len(new_text) < 10:
        await message.answer("Текст слишком короткий. Отправьте полный текст объявления.")
        return

    publication = await _owned_publication(message.from_user.id, int(publication_id))
    if not publication:
        await state.clear()
        await message.answer("Публикация не найдена.", reply_markup=inline_home_row())
        return

    allowed, reason = await _fraud_check_allows(publication, new_text)
    if not allowed:
        await message.answer(reason, reply_markup=inline_home_row())
        await state.clear()
        return

    async with SessionLocal() as session:
        await get_or_create_user(session, message.from_user)

    bot = message.bot
    updated, failed, pending = await apply_publication_edit(bot, int(publication_id), new_text)
    await state.clear()

    lines = ["<b>✅ Объявление обновлено</b>", ""]
    if updated:
        lines.append(f"✏️ Изменено сообщений: <b>{updated}</b>")
    if pending:
        lines.append(f"⏳ В очереди (выйдет уже новый текст): <b>{pending}</b>")
    if failed:
        lines.append(
            f"⚠️ Не удалось изменить: <b>{failed}</b> — эти посты опубликованы "
            "ботом-партнёром, правка на его стороне не проходит.",
        )
    await message.answer("\n".join(lines), reply_markup=inline_home_row())

    await refresh_publication_status(bot, int(publication_id))


async def _fraud_check_allows(publication: Publication, new_text: str) -> tuple[bool, str]:
    """Правка обычно минует проверку сходства — иначе кнопка нерабочая по определению.

    Поведение переключается настройкой EDIT_SKIPS_FRAUD_CHECK.
    """
    from app.config import get_settings

    if get_settings().edit_skips_fraud_check or not publication.subscription_id:
        return True, ""

    from app.models.entities import Subscription
    from app.services.fraud import check_publish_allowed

    async with SessionLocal() as session:
        sub = await session.scalar(
            select(Subscription).where(Subscription.id == publication.subscription_id),
        )
    if not sub:
        return True, ""

    ok, score, _ = check_publish_allowed(sub.approved_text, sub.approved_text_hash, new_text)
    if ok:
        return True, ""
    return False, (
        "<b>⛔ Текст отклонён</b>\n\n"
        f"Сходство с одобренным: <b>{score:.0%}</b> (нужно ≥ 90%).\n\n"
        "Для нового объявления оформите подписку заново."
    )


@publication_actions_router.callback_query(F.data.startswith("pub:close:"))
async def publication_close(callback: CallbackQuery) -> None:
    publication_id = int(callback.data.split(":")[2])
    publication = await _owned_publication(callback.from_user.id, publication_id)
    if not publication:
        await callback.answer("Публикация не найдена", show_alert=True)
        return
    if publication.closed:
        await callback.answer("Объявление уже закрыто", show_alert=True)
        return

    closed, failed, cancelled = await apply_publication_close(callback.bot, publication_id)

    # Итог показываем всплывающим окном и перерисованной карточкой публикации:
    # пользователь нажал кнопку и текст не вводил, новое сообщение здесь лишнее.
    parts = ["🚫 Объявление закрыто"]
    if closed:
        parts.append(f"зачёркнуто сообщений: {closed}")
    if cancelled:
        parts.append(f"снято из очереди: {cancelled}")
    if failed:
        parts.append(f"не удалось закрыть: {failed} — опубликованы ботом-партнёром")

    await refresh_publication_status(callback.bot, publication_id)
    try:
        await callback.answer(", ".join(parts), show_alert=True)
    except Exception:
        logger.debug("Не удалось показать итог закрытия", exc_info=True)
