"""Раздел «Публикации» (ТЗ этапа 2, 6.9).

Кадры макета: `340:733` — сводка, `340:751` / `340:892` / `340:1024` —
три фильтрованных списка, `340:863` / `340:982` / `340:1079` — карточка
публикации с действиями по её статусу.

Работаем на уровне задачи публикации (`PublishJob`): именно у неё есть
группа, позиция в очереди и причина ошибки. Действия — в
`app/services/queue_admin.py`.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc, func, select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.common import LIST_PER_PAGE, _is_admin
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_DANGER, STYLE_MAIN, STYLE_PLAIN
from app.models.entities import Chat, City, Publication, PublishJob, User
from app.services import admin_audit, admin_stats, admin_ui, banners, queue_admin
from app.services.publication_status import apply_publication_edit
from app.services.publish_errors import human_reason
from app.services.textfmt import (
    eta_label,
    fit_label,
    moment_label,
    short_moment,
    until_label,
)
from app.states.admin import AdminFlow

logger = logging.getLogger("admin")
router = Router()

# Экран уходит подписью к баннеру — она ограничена 1024 символами.
TEXT_PREVIEW = 500


def _short(text: str) -> str:
    clean = (text or "").strip()
    return clean[:TEXT_PREVIEW] + "…" if len(clean) > TEXT_PREVIEW else clean

PUBS_CB = "adm:pubs"
LIST_CB = "adm:pubs:list:"      # + <вид>:<страница>
CARD_CB = "adm:pubs:card:"      # + <id задачи>
ACTION_CB = "adm:pubs:act:"     # + <действие>:<id задачи>
# Правка текста нарочно вне ACTION_CB: у обработчика действий неизвестное
# действие падает в ветку «отменить», и `adm:pubs:act:edit:*` снял бы задачу
# с очереди вместо правки.
EDIT_CB = "adm:pubs:edit:"      # + <id задачи>

VIEW_QUEUED = "queued"
VIEW_PUBLISHED = "published"
VIEW_FAILED = "failed"

_VIEWS = {
    VIEW_QUEUED: (T.PUBS_LIST_QUEUED, ("queued",)),
    VIEW_PUBLISHED: (T.PUBS_LIST_PUBLISHED, ("published",)),
    VIEW_FAILED: (T.PUBS_LIST_FAILED, admin_stats.FAILED_STATUSES),
}


@router.callback_query(F.data == PUBS_CB)
async def admin_publications(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    summary = await admin_stats.publications_summary()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=T.BTN_PUBS_QUEUED, callback_data=f"{LIST_CB}{VIEW_QUEUED}:0", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_PUBS_PUBLISHED, callback_data=f"{LIST_CB}{VIEW_PUBLISHED}:0", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_PUBS_FAILED, callback_data=f"{LIST_CB}{VIEW_FAILED}:0", style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(callback, T.PUBLICATIONS.format(**summary), markup)


async def job_label(session, job: PublishJob) -> str:
    """Подпись строки списка: только «#номер · @ник».

    Город, чат и статус в кнопку не помещаются (обрезались «Санкт-Пете…») и
    видны в карточке по нажатию (заказчик 21.09.2026).
    """
    author = (
        await session.scalar(select(User).where(User.id == job.author_user_id))
        if job.author_user_id
        else None
    )
    who = f"@{author.username}" if author and author.username else "—"
    return f"#{job.id} · {fit_label(who)}"


def _list_query(view: str, statuses):
    """Записи списка. «Опубликованные» и «Ошибки» — только за текущие сутки."""
    day_start, _ = admin_stats.period_bounds(admin_stats.PERIOD_TODAY)
    query = select(PublishJob).where(PublishJob.status.in_(statuses))
    if view == VIEW_PUBLISHED:
        query = query.where(PublishJob.published_at >= day_start)
    elif view == VIEW_FAILED:
        query = query.where(func.coalesce(PublishJob.failed_at, PublishJob.scheduled_at) >= day_start)
    return query.order_by(desc(PublishJob.id))


@router.callback_query(F.data.startswith(LIST_CB))
async def admin_publications_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    _, view, raw_page = callback.data.rsplit(":", 2)
    template, statuses = _VIEWS.get(view, _VIEWS[VIEW_QUEUED])

    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    _list_query(view, statuses),
                )
            ).all(),
        )
        shown, page, pages = page_slice(rows, int(raw_page), LIST_PER_PAGE)
        buttons = [
            [
                InlineKeyboardButton(
                    text=await job_label(session, job),
                    callback_data=f"{CARD_CB}{job.id}",
                    style=STYLE_PLAIN,
                ),
            ]
            for job in shown
        ]

    pager = pager_row(f"{LIST_CB}{view}:", page, pages)
    if pager:
        buttons.append(pager)
    buttons.append([admin_ui.back_button(PUBS_CB)])
    # Кадры 340:751 / 340:892 / 340:1024: под своим заголовком каждый список
    # повторяет всю сводку раздела, а не счётчик собственных строк.
    text = template.format(**await admin_stats.publications_summary())
    if not rows:
        text += T.PUBS_LIST_EMPTY
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))


async def _position_in_queue(session, job: PublishJob) -> int:
    """Позиция среди ждущих задач той же группы — как в макете.

    Очерёдность внутри группы задаётся id, а не `scheduled_at`: последний
    лишь гейт «не раньше чем» и сдвигается при тишине, паузе и ретраях.
    """
    return await session.scalar(
        select(func.count())
        .select_from(PublishJob)
        .where(
            PublishJob.chat_id == job.chat_id,
            PublishJob.status == "queued",
            PublishJob.id <= job.id,
        ),
    ) or 1


async def _card(session, job: PublishJob) -> tuple[str, InlineKeyboardMarkup]:
    publication = None
    if job.publication_id:
        publication = await session.scalar(select(Publication).where(Publication.id == job.publication_id))
    chat = await session.scalar(select(Chat).where(Chat.id == job.chat_id))
    city = await session.scalar(select(City).where(City.id == chat.city_id)) if chat else None
    author = await session.scalar(select(User).where(User.id == job.author_user_id)) if job.author_user_id else None

    if job.status == "queued":
        status_block = T.PUB_STATUS_QUEUED.format(
            position=await _position_in_queue(session, job),
            # Кадр 340:863 подписывает ожидание словами «через 2 мин», а не
            # датой: до отправки обычно минуты, и дата тут ничего не говорит.
            next_at=until_label(job.scheduled_at),
        )
        actions = [
            [InlineKeyboardButton(text=T.BTN_PUB_NOW, callback_data=f"{ACTION_CB}now:{job.id}", style=STYLE_MAIN)],
            [InlineKeyboardButton(text=T.BTN_PUB_CANCEL, callback_data=f"{ACTION_CB}cancel:{job.id}", style=STYLE_DANGER)],
        ]
        back = f"{LIST_CB}{VIEW_QUEUED}:0"
    elif job.status == "published":
        status_block = T.PUB_STATUS_PUBLISHED.format(published_at=moment_label(job.published_at))
        # «Изменить» правит публикацию целиком, во всех её группах, поэтому
        # кнопка есть только у задач, привязанных к публикации: у разовых и
        # пакетных постов `publication_id` пустой, править нечего.
        actions = (
            [
                [
                    InlineKeyboardButton(
                        text=T.BTN_PUB_EDIT,
                        callback_data=f"{EDIT_CB}{job.id}",
                        style=STYLE_MAIN,
                    ),
                ],
            ]
            if job.publication_id
            else []
        )
        back = f"{LIST_CB}{VIEW_PUBLISHED}:0"
    elif job.status == "cancelled":
        status_block = T.PUB_STATUS_CANCELLED
        actions = []
        back = f"{LIST_CB}{VIEW_QUEUED}:0"
    else:
        status_block = T.PUB_STATUS_FAILED.format(
            reason=human_reason(job.error or ""),
            failed_at=moment_label(job.failed_at or job.scheduled_at),
        )
        actions = [
            [InlineKeyboardButton(text=T.BTN_PUB_RETRY, callback_data=f"{ACTION_CB}retry:{job.id}", style=STYLE_MAIN)],
            [InlineKeyboardButton(text=T.BTN_PUB_DROP, callback_data=f"{ACTION_CB}cancel:{job.id}", style=STYLE_DANGER)],
        ]
        back = f"{LIST_CB}{VIEW_FAILED}:0"

    number = publication.id if publication else job.id
    text = T.PUB_CARD.format(
        title=f"{'❌' if job.status in ('failed', 'expired') else '📋'} Публикация #{number}",
        author=f"@{author.username}" if author and author.username else (author.full_name if author else "—"),
        telegram_id=author.telegram_id if author else "—",
        city=city.label if city else "—",
        chat=chat.title if chat else "—",
        status_block=status_block,
    )
    actions.append([admin_ui.back_button(back)])
    return text, InlineKeyboardMarkup(inline_keyboard=actions)


@router.callback_query(F.data.startswith(CARD_CB))
async def admin_publication_card(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    job_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        job = await session.scalar(select(PublishJob).where(PublishJob.id == job_id))
        if job is None:
            await callback.answer("Публикация не найдена", show_alert=True)
            return
        text, markup = await _card(session, job)
    await callback.answer()
    await admin_ui.show(callback, text, markup)


@router.callback_query(F.data.startswith(EDIT_CB))
async def admin_publication_edit_start(callback: CallbackQuery, state: FSMContext) -> None:
    """«Изменить» у опубликованной публикации (ТЗ 6.9, кадр 340:982)."""
    if not _is_admin(callback.from_user.id):
        return
    job_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        job = await session.scalar(select(PublishJob).where(PublishJob.id == job_id))
        publication = (
            await session.scalar(select(Publication).where(Publication.id == job.publication_id))
            if job and job.publication_id
            else None
        )
    if publication is None:
        await callback.answer("Публикация не найдена", show_alert=True)
        return

    await state.set_state(AdminFlow.publication_text)
    await state.update_data(pub_edit_id=publication.id, pub_edit_job_id=job_id)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.PUB_EDIT_ASK.format(number=publication.id, text=_short(publication.text)),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(f"{CARD_CB}{job_id}")]]),
    )


@router.message(AdminFlow.publication_text)
async def admin_publication_edit_save(message: Message, state: FSMContext) -> None:
    """Новый текст от администратора.

    Проверку сходства с одобренным текстом здесь не делаем: она стоит на
    правке автором, чтобы тот не подменил объявление после оплаты, а
    администратор правит осознанно.
    """
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    publication_id = data.get("pub_edit_id")
    job_id = data.get("pub_edit_job_id")
    back = InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(f"{CARD_CB}{job_id}")]])
    if not publication_id:
        await state.clear()
        return

    new_text = (message.text or "").strip()
    if len(new_text) < 10:
        await admin_ui.show(message, T.PUB_EDIT_SHORT, back, edit=False)
        return

    await state.clear()
    updated, failed, pending = await apply_publication_edit(message.bot, int(publication_id), new_text)
    await admin_audit.log_action(
        message.from_user.id,
        admin_audit.PUBLICATION_ACTION,
        target=f"publication#{publication_id}",
        detail=f"edit updated={updated} failed={failed} pending={pending}",
    )

    lines = []
    if updated:
        lines.append(T.PUB_EDITED_UPDATED.format(count=updated))
    if pending:
        lines.append(T.PUB_EDITED_PENDING.format(count=pending))
    if failed:
        lines.append(T.PUB_EDITED_FAILED.format(count=failed))
    if not lines:
        lines.append(T.PUB_EDITED_NOTHING)

    await admin_ui.show(
        message,
        T.PUB_EDITED.format(number=publication_id, lines="\n".join(lines)),
        back,
        edit=False,
    )
    await _notify_author(message.bot, int(publication_id), new_text)


async def _notify_author(bot, publication_id: int, new_text: str) -> None:
    """Сказать автору, что текст правил не он.

    `refresh_publication_status` тут молчит: статус публикации не изменился, а
    он шлёт уведомление только на смену состояния. Поэтому отдельное
    сообщение, иначе автор об изменении просто не узнает.
    """
    async with SessionLocal() as session:
        publication = await session.scalar(
            select(Publication).where(Publication.id == publication_id),
        )
        author = (
            await session.scalar(select(User).where(User.id == publication.user_id))
            if publication
            else None
        )
    if author is None or not author.telegram_id:
        return
    try:
        await bot.send_message(
            author.telegram_id,
            T.PUB_EDITED_BY_ADMIN.format(number=publication_id, text=_short(new_text)),
        )
    except Exception:
        logger.info("Не удалось уведомить автора %s о правке", author.telegram_id, exc_info=True)
    else:
        # Наше сообщение стало последним в его чате — экран раздела больше
        # не перерисовать, иначе правка уедет в чужое сообщение.
        banners.forget_screen(author.telegram_id)


@router.callback_query(F.data.startswith(ACTION_CB))
async def admin_publication_action(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    _, action, raw_id = callback.data.rsplit(":", 2)
    job_id = int(raw_id)
    actor = callback.from_user.id

    try:
        if action == "now":
            await queue_admin.publish_now(job_id, actor_telegram_id=actor)
            note = "Публикуем вне очереди"
        elif action == "retry":
            await queue_admin.retry_job(job_id, actor_telegram_id=actor)
            note = "Вернули в очередь"
        else:
            await queue_admin.cancel_job(job_id, actor_telegram_id=actor)
            note = "Отменено"
    except queue_admin.JobActionError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await admin_audit.log_action(
        actor, admin_audit.PUBLICATION_ACTION, target=f"job#{job_id}", detail=action,
    )
    await callback.answer(note)

    async with SessionLocal() as session:
        job = await session.scalar(select(PublishJob).where(PublishJob.id == job_id))
        text, markup = await _card(session, job)
    await admin_ui.show(callback, text, markup)
