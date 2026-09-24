"""Раздел «Очередь» и её настройки (ТЗ этапа 2, 6.10)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.handlers.admin.common import QUEUE_PER_PAGE, _is_admin
from app.handlers.admin.publications import job_label
from app.keyboards.style import STYLE_ACTIVE, STYLE_PLAIN
from app.models.entities import Chat, PublishJob
from app.services import admin_audit, admin_ui, queue_admin, settings_store
from app.services.textfmt import minutes_label, to_local

router = Router()


QUEUE_CB = "adm:queue"
SETTINGS_CB = "adm:queue:set"
SILENCE_CB = "adm:queue:silence"
INTERVAL_CB = "adm:queue:interval"
SET_SILENCE_CB = "adm:queue:silence:"    # + значение в минутах
SET_INTERVAL_CB = "adm:queue:interval:"  # + значение в минутах


async def _busy_chat_slots(session) -> list[str]:
    """Группы, которые сейчас в тишине или на паузе (ТЗ 6.4)."""
    from datetime import UTC, datetime

    from app.models.entities import ChatSlotState
    from app.services.queue_slots import SLOT_FREE, SLOT_SILENCE, slot_status

    now = datetime.now(UTC)
    pause_sec = await settings_store.queue_user_pause_sec()
    lines: list[str] = []
    for slot in (await session.scalars(select(ChatSlotState))).all():
        state, ready_at = slot_status(slot, now, pause_sec)
        if state == SLOT_FREE:
            continue
        chat = await session.scalar(select(Chat).where(Chat.id == slot.chat_id))
        title = chat.title if chat else f"chat {slot.chat_id}"
        label = "🔇 тишина" if state == SLOT_SILENCE else "⏸ пауза"
        lines.append(f"• {title} — {label} до {to_local(ready_at):%H:%M}")
    return lines


@router.callback_query(F.data == QUEUE_CB)
async def admin_queue(callback: CallbackQuery) -> None:
    """Список ждущих публикаций — кадр макета 341:1112."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        queued = list(
            (
                await session.scalars(
                    select(PublishJob)
                    .where(PublishJob.status == "queued")
                    .order_by(PublishJob.scheduled_at, PublishJob.id),
                )
            ).all(),
        )
        busy = await _busy_chat_slots(session)
        rows = []
        for job in queued[:QUEUE_PER_PAGE]:
            # Подпись строит тот же помощник, что и списки публикаций: очередь
            # в макете (341:1112) и «В очереди» (340:751) — один и тот же вид.
            rows.append([
                InlineKeyboardButton(
                    text=await job_label(session, job),
                    callback_data=f"adm:pubs:card:{job.id}",
                    style=STYLE_PLAIN,
                ),
            ])

    text = T.QUEUE.format(count=len(queued)) if queued else T.QUEUE_EMPTY
    if busy:
        text += "\n\n<b>Состояние групп:</b>\n" + "\n".join(busy)

    # В макете (341:1112) эти кнопки стоят одним рядом, но в половину ряда
    # помещается 16 знакомест, а в подписях 20 и 17 — хвост обрежется. По
    # правилу заказчика («не влезает — правим макет») оставляем по одной в
    # ряд, а пару в Figma разводим; см. docs/figma_fixes_pending.md.
    rows.append([
        InlineKeyboardButton(text=T.BTN_QUEUE_SETTINGS, callback_data=SETTINGS_CB, style=STYLE_PLAIN),
    ])
    rows.append([
        InlineKeyboardButton(text=T.BTN_QUEUE_ALL_PUBS, callback_data="adm:pubs", style=STYLE_PLAIN),
    ])
    rows.append([admin_ui.panel_button()])
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == SETTINGS_CB)
async def admin_queue_settings(callback: CallbackQuery) -> None:
    """«Настройки очереди» — кадр макета 341:1222."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    text = T.QUEUE_SETTINGS.format(
        silence=minutes_label(await settings_store.queue_silence_minutes()),
        pause=minutes_label(await settings_store.queue_user_pause_sec() // 60),
    )
    # Пара в один ряд — как на кадре. Обе подписи в половину ряда помещаются
    # («Тихий режим» 14 знакомест, «Интервал» 11 при пределе 16), так что
    # разносить их было незачем: это расхождение с макетом нашла сверка сеток
    # 20.09.2026, до неё ряды кнопок не проверял ни один тест.
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=T.BTN_QUEUE_SILENCE, callback_data=SILENCE_CB, style=STYLE_PLAIN),
                InlineKeyboardButton(text=T.BTN_QUEUE_INTERVAL, callback_data=INTERVAL_CB, style=STYLE_PLAIN),
            ],
            [admin_ui.back_button(QUEUE_CB)],
        ],
    )
    await admin_ui.show(callback, text, markup)


def _choice_rows(choices, current: int, prefix: str) -> list[list[InlineKeyboardButton]]:
    """Варианты значения по два в ряд; выбранный залит синим — как в макете.

    Сетка не косметика: кадры 341:1248 и 344:1284 рисуют кнопки парами, и
    последняя из нечётного числа занимает ряд целиком. Пять кнопок в одну
    строку Telegram сжимает так, что «1 мин» и «5 мин» становятся нечитаемы на
    узком экране. Одним рядом они стояли и в нашей копии макета — это была
    наша правка от 17.09.2026, заказчик её отменил 20.09.2026.
    """
    buttons = [
        InlineKeyboardButton(
            text=f"{value} мин",
            callback_data=f"{prefix}{value}",
            style=STYLE_ACTIVE if value == current else STYLE_PLAIN,
        )
        for value in choices
    ]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


@router.callback_query(F.data == SILENCE_CB)
async def admin_queue_silence(callback: CallbackQuery) -> None:
    """Выбор длительности тихого режима — кадр макета 344:1284."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    current = await settings_store.queue_silence_minutes()
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            *_choice_rows(T.SILENCE_CHOICES, current, SET_SILENCE_CB),
            [admin_ui.back_button(SETTINGS_CB)],
        ],
    )
    await admin_ui.show(callback, T.QUEUE_SILENCE.format(current=minutes_label(current)), markup)


@router.callback_query(F.data == INTERVAL_CB)
async def admin_queue_interval(callback: CallbackQuery) -> None:
    """Выбор интервала между публикациями — кадр макета 341:1248."""
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    current = await settings_store.queue_user_pause_sec() // 60
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            *_choice_rows(T.INTERVAL_CHOICES, current, SET_INTERVAL_CB),
            [admin_ui.back_button(SETTINGS_CB)],
        ],
    )
    await admin_ui.show(callback, T.QUEUE_INTERVAL.format(current=minutes_label(current)), markup)


@router.callback_query(F.data.startswith(SET_SILENCE_CB))
async def admin_queue_set_silence(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    value = int(callback.data.rsplit(":", 1)[-1])
    if value not in T.SILENCE_CHOICES:
        await callback.answer("Недопустимое значение", show_alert=True)
        return
    await settings_store.set_int(
        settings_store.QUEUE_SILENCE_MINUTES, value, by_telegram_id=callback.from_user.id,
    )
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.QUEUE_SETTING, target="silence_minutes", detail=str(value),
    )
    await callback.answer("Сохранено")
    await admin_ui.show(
        callback,
        T.QUEUE_SAVED_SILENCE.format(value=minutes_label(value)),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(SETTINGS_CB)]]),
    )


@router.callback_query(F.data.startswith(SET_INTERVAL_CB))
async def admin_queue_set_interval(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    value = int(callback.data.rsplit(":", 1)[-1])
    if value not in T.INTERVAL_CHOICES:
        await callback.answer("Недопустимое значение", show_alert=True)
        return
    await settings_store.set_int(
        settings_store.QUEUE_USER_PAUSE_SEC, value * 60, by_telegram_id=callback.from_user.id,
    )
    await admin_audit.log_action(
        callback.from_user.id, admin_audit.QUEUE_SETTING, target="user_pause_sec", detail=str(value * 60),
    )
    await callback.answer("Сохранено")
    await admin_ui.show(
        callback,
        T.QUEUE_SAVED_INTERVAL.format(value=minutes_label(value)),
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(SETTINGS_CB)]]),
    )


@router.message(Command("silence"))
async def admin_silence(message: Message) -> None:
    """Ручная установка тишины, пока Hammer/W не дёргают вебхук (ТЗ 6.6)."""
    if not _is_admin(message.from_user.id):
        return
    from app.services.oneshot import register_oneshot_publication

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "<b>🔇 Ручная тишина группы</b>\n\n"
            "<code>/silence &lt;chat_id|@username&gt; [минут]</code>\n\n"
            "Ставит то же окно тишины, что и разовый пост из Hammer/W.",
        )
        return

    target = parts[1].strip()
    minutes = None
    if len(parts) > 2 and parts[2].isdigit():
        minutes = int(parts[2])

    telegram_chat_id: int | None = None
    telegram_username: str | None = None
    if target.startswith("@") or not target.lstrip("-").isdigit():
        telegram_username = target
    else:
        telegram_chat_id = int(target)
        async with SessionLocal() as session:
            by_db_id = await session.scalar(select(Chat).where(Chat.id == telegram_chat_id))
        if by_db_id and by_db_id.telegram_chat_id:
            telegram_chat_id = int(by_db_id.telegram_chat_id)

    async with SessionLocal() as session:
        result = await register_oneshot_publication(
            session,
            telegram_chat_id=telegram_chat_id,
            telegram_username=telegram_username,
            network="manual",
            minutes=minutes,
        )

    if not result.get("ok"):
        await message.answer(f"❌ Группа не найдена: <code>{target}</code>")
        return
    await message.answer(
        f"🔇 <b>{result.get('chat_title') or target}</b> — тишина до "
        f"<b>{result['silence_until'][11:16]}</b> UTC.\n"
        "Посты подписчиков и белого списка встанут в очередь и выйдут после.",
    )


@router.callback_query(F.data == "adm:queue:retry")
async def admin_queue_retry(callback: CallbackQuery) -> None:
    """Массовый повтор всех упавших задач.

    Раньше был написан прямо здесь и не писал ни события очереди, ни журнал
    действий — теперь и то, и другое делает services/queue_admin.
    """
    if not _is_admin(callback.from_user.id):
        return
    count = await queue_admin.retry_all_failed(actor_telegram_id=callback.from_user.id)
    await admin_audit.log_action(
        callback.from_user.id,
        admin_audit.PUBLICATION_ACTION,
        target="all_failed",
        detail=f"retry×{count}",
    )
    await callback.answer(f"В очередь: {count}")
    await admin_ui.show(
        callback,
        f"<b>🔁 Повторно в очередь</b>\n\nЗадач: <b>{count}</b>",
        InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(QUEUE_CB)]]),
    )
