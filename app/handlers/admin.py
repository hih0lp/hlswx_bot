from __future__ import annotations

import json
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.enums import ButtonStyle
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import desc, func, select

from app.config import get_settings
from app.db.session import SessionLocal
from app.keyboards.admin import (
    admin_back_keyboard,
    admin_log_keyboard,
    admin_main_keyboard,
    admin_ml_category_keyboard,
    admin_ml_keyboard,
    admin_queue_keyboard,
    admin_retrain_offer_keyboard,
    admin_wl_keyboard,
    admin_wlreq_keyboard,
    admin_wlreq_view_keyboard,
    admin_wlbl_keyboard,
)
from app.ml.categories import CATEGORIES
from app.ml.classifier import get_classifier
from app.ml.import_samples import parse_photo_caption, parse_training_file, supported_formats_hint
from app.ml.train import train_and_save
from app.models.entities import (
    City,
    Chat,
    ClassificationLog,
    PackageOrder,
    Payment,
    PaymentStatus,
    PublishJob,
    Subscription,
    SubscriptionStatus,
    TrainingSample,
    User,
    WhitelistEntry,
    WhitelabelApplication,
    WhitelabelApplicationStatus,
    WhitelabelPartner,
)
from app.services.admin_stats import gather_dashboard_stats
from app.services.whitelist import parse_chat_ids, parse_city_keys, provision_whitelist_subscription
from app.states.admin import AdminFlow

logger = logging.getLogger("admin")
admin_router = Router()


def _is_admin(user_id: int) -> bool:
    return user_id in get_settings().admin_ids_set


async def _wl_pending_count() -> int:
    async with SessionLocal() as session:
        return await session.scalar(
            select(func.count())
            .select_from(WhitelabelApplication)
            .where(WhitelabelApplication.status == WhitelabelApplicationStatus.pending),
        ) or 0


def _format_wl_application(app: WhitelabelApplication) -> str:
    status_map = {
        WhitelabelApplicationStatus.pending: "⏳ ожидает",
        WhitelabelApplicationStatus.approved: "✅ одобрена",
        WhitelabelApplicationStatus.rejected: "❌ отклонена",
    }
    uname = f"@{app.username}" if app.username else "—"
    bot_line = f"\nБот: @{app.planned_bot_username}" if app.planned_bot_username else ""
    comment = f"\n\n💬 {app.comment}" if app.comment else ""
    note = f"\n\n<i>Админ: {app.admin_note}</i>" if app.admin_note else ""
    created = app.created_at.strftime("%d.%m.%Y %H:%M") if app.created_at else "—"
    return (
        f"<b>📝 Заявка #{app.id}</b> — {status_map.get(app.status, app.status.value)}\n"
        f"От: {uname} · <code>{app.telegram_id}</code>\n"
        f"Бренд: <b>{app.brand_title}</b>{bot_line}{comment}{note}\n\n"
        f"Создана: {created}"
    )


async def _panel_text() -> str:
    stats = await gather_dashboard_stats()
    clf = get_classifier()
    meta = clf.meta or {}
    return (
        "🛠 <b>Админ-панель HWLS</b>\n"
        "╭──────────────────────╮\n\n"
        f"👥 Пользователей: <b>{stats['users']}</b>\n"
        f"📅 Активных подписок: <b>{stats['active_subs']}</b>\n"
        f"⏳ Ждут оплаты: подписки <b>{stats['pending_subs']}</b> · пакеты <b>{stats['pending_pkgs']}</b>\n"
        f"💳 Платежей pending: <b>{stats['pending_payments']}</b>\n"
        f"📬 Очередь: в работе <b>{stats['queue_queued']}</b> · ошибки <b>{stats['queue_failed']}</b>\n"
        f"👥 Whitelist: <b>{stats['whitelist']}</b>\n"
        f"📝 WL заявки: <b>{stats['wl_pending']}</b>\n"
        f"⚠️ Логов на проверку: <b>{stats['review_logs']}</b>\n\n"
        f"🧠 ML: {'✅' if clf.is_ready else '❌'} · "
        f"{meta.get('samples', '—')} прим. · acc {meta.get('accuracy', 0):.0%}"
    )


async def _show_panel(target: Message) -> None:
    await target.answer(await _panel_text(), reply_markup=admin_main_keyboard())


@admin_router.message(Command("admin"))
async def admin_menu(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await state.clear()
    await _show_panel(message)


@admin_router.callback_query(F.data == "adm:home")
async def admin_home(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    text = await _panel_text()
    try:
        await callback.message.edit_text(text, reply_markup=admin_main_keyboard())
    except Exception:
        await callback.message.answer(text, reply_markup=admin_main_keyboard())


@admin_router.callback_query(F.data == "adm:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    stats = await gather_dashboard_stats()
    await callback.message.answer(
        "<b>📊 Статистика</b>\n"
        "╭──────────────────────╮\n\n"
        f"👥 Пользователи: <b>{stats['users']}</b>\n"
        f"✅ Активные подписки: <b>{stats['active_subs']}</b>\n"
        f"⏳ Подписки ждут оплаты: <b>{stats['pending_subs']}</b>\n"
        f"⏳ Пакеты ждут оплаты: <b>{stats['pending_pkgs']}</b>\n"
        f"💳 Pending платежи: <b>{stats['pending_payments']}</b>\n"
        f"📬 Очередь (queued): <b>{stats['queue_queued']}</b>\n"
        f"❌ Очередь (failed): <b>{stats['queue_failed']}</b>\n"
        f"👥 Whitelist: <b>{stats['whitelist']}</b>\n"
        f"⚠️ OTHER без вердикта: <b>{stats['review_logs']}</b>",
        reply_markup=admin_back_keyboard(),
    )


@admin_router.callback_query(F.data == "adm:payments")
async def admin_payments(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        subs = (
            await session.scalars(
                select(Subscription)
                .where(Subscription.status == SubscriptionStatus.pending_payment)
                .order_by(desc(Subscription.id))
                .limit(8),
            )
        ).all()
        pkgs = (
            await session.scalars(
                select(PackageOrder).where(PackageOrder.status == "pending_payment").order_by(desc(PackageOrder.id)).limit(8),
            )
        ).all()
        pays = (
            await session.scalars(
                select(Payment).where(Payment.status == PaymentStatus.pending).order_by(desc(Payment.id)).limit(8),
            )
        ).all()

    lines = ["<b>💳 Ожидают оплаты</b>\n╭──────────────────────╮\n"]
    if subs:
        lines.append("<b>Подписки:</b>")
        for s in subs:
            lines.append(f"• #{s.id} — {s.total_price:,} ₽")
    if pkgs:
        lines.append("\n<b>Пакеты:</b>")
        for p in pkgs:
            lines.append(f"• #{p.id} — {p.price:,} ₽")
    if pays:
        lines.append("\n<b>Платежи:</b>")
        for p in pays:
            lines.append(f"• #{p.id} {p.purpose} #{p.purpose_id} — {p.amount} ₽")
    if not subs and not pkgs and not pays:
        lines.append("\n<i>Нет ожидающих оплат</i>")
    lines.append(
        "\n\n💡 Webhook ЮKassa:\n"
        "<code>http://161.104.47.79:8082/payments/yookassa/webhook</code>\n"
        "<i>Резервная проверка — каждые 90 сек</i>"
    )
    await callback.message.answer("\n".join(lines), reply_markup=admin_back_keyboard())


@admin_router.callback_query(F.data == "adm:queue")
async def admin_queue(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        queued = (
            await session.scalars(
                select(PublishJob).where(PublishJob.status == "queued").order_by(PublishJob.scheduled_at).limit(5),
            )
        ).all()
        failed = (
            await session.scalars(
                select(PublishJob).where(PublishJob.status == "failed").order_by(desc(PublishJob.id)).limit(5),
            )
        ).all()

        expired = (
            await session.scalars(
                select(PublishJob).where(PublishJob.status == "expired").order_by(desc(PublishJob.id)).limit(5),
            )
        ).all()
        busy_slots = await _busy_chat_slots(session)

    lines = ["<b>📬 Очередь публикаций</b>\n╭──────────────────────╮\n"]
    if busy_slots:
        lines.append("<b>Состояние групп:</b>")
        lines.extend(busy_slots)
    if queued:
        lines.append("\n<b>В очереди:</b>")
        for j in queued:
            lines.append(f"• job #{j.id} · chat {j.chat_id} · {j.scheduled_at.strftime('%H:%M')}")
    if failed:
        lines.append("\n<b>Ошибки:</b>")
        for j in failed:
            err = (j.error or "—")[:60]
            lines.append(f"• job #{j.id} — {err}")
    if expired:
        lines.append("\n<b>Протухли в очереди:</b>")
        for j in expired:
            lines.append(f"• job #{j.id} · chat {j.chat_id}")
    if not queued and not failed and not expired and not busy_slots:
        lines.append("\n<i>Очередь пуста, все группы свободны</i>")
    lines.append("\n<i>Ручная тишина: /silence &lt;chat_id|@username&gt; [минут]</i>")
    await callback.message.answer("\n".join(lines), reply_markup=admin_queue_keyboard())


async def _busy_chat_slots(session) -> list[str]:
    """Группы, которые сейчас в тишине или на паузе (ТЗ 6.4)."""
    from datetime import UTC, datetime

    from app.models.entities import ChatSlotState
    from app.services.queue_slots import SLOT_FREE, SLOT_SILENCE, slot_status

    now = datetime.now(UTC)
    pause_sec = get_settings().queue_user_pause_sec
    lines: list[str] = []
    for slot in (await session.scalars(select(ChatSlotState))).all():
        state, ready_at = slot_status(slot, now, pause_sec)
        if state == SLOT_FREE:
            continue
        chat = await session.scalar(select(Chat).where(Chat.id == slot.chat_id))
        title = chat.title if chat else f"chat {slot.chat_id}"
        label = "🔇 тишина" if state == SLOT_SILENCE else "⏸ пауза"
        lines.append(f"• {title} — {label} до {ready_at.strftime('%H:%M')}")
    return lines


@admin_router.message(Command("silence"))
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


@admin_router.callback_query(F.data == "adm:queue:retry")
async def admin_queue_retry(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        failed = (
            await session.scalars(
                select(PublishJob).where(PublishJob.status == "failed", PublishJob.attempts < 3),
            )
        ).all()
        count = 0
        now = datetime.now(UTC)
        for job in failed:
            job.status = "queued"
            job.scheduled_at = now
            job.attempts += 1
            job.error = None
            count += 1
        await session.commit()
    await callback.answer(f"В очередь: {count}")
    await callback.message.answer(f"🔁 Повторно в очередь: <b>{count}</b> задач", reply_markup=admin_back_keyboard())


@admin_router.callback_query(F.data == "adm:ml")
async def admin_ml_menu(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.clear()
    await callback.answer()
    async with SessionLocal() as session:
        db_count = await session.scalar(select(func.count()).select_from(TrainingSample)) or 0
    clf = get_classifier()
    meta = clf.meta or {}
    await callback.message.answer(
        "<b>🧠 ML — дообучение</b>\n"
        "╭──────────────────────╮\n\n"
        f"📂 Excel: <b>{meta.get('samples', '—')}</b> примеров (при последнем обучении)\n"
        f"➕ Добавлено админом в БД: <b>{db_count}</b>\n\n"
        "<b>Как доучивать:</b>\n"
        "1️⃣ <b>Добавить пример</b> — текст, файл или 📷 фото (OCR)\n"
        "2️⃣ <b>Загрузить файл</b> — .xlsx / .csv / .json / .zip / картинки\n"
        "3️⃣ <b>Логи</b> — исправить ошибки бота (тоже сохраняется)\n"
        "4️⃣ <b>Переобучить</b> — Excel + все примеры из БД\n\n"
        "💡 Новые примеры попадают в модель только после <b>переобучения</b>.",
        reply_markup=admin_ml_keyboard(),
    )


@admin_router.callback_query(F.data == "adm:ml:add")
async def admin_ml_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.ml_sample_text)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Новый пример для ML</b>\n\n"
        "Пришлите:\n"
        "• <b>текст</b> объявления\n"
        "• <b>файл</b> (.xlsx / .csv / .json / .zip / изображение)\n"
        "• <b>фото</b> скрина — подпись <code>VACANCY</code> или <code>VACANCY\\nтекст</code>",
        reply_markup=admin_back_keyboard(),
    )


@admin_router.message(AdminFlow.ml_sample_text, F.photo)
async def admin_ml_add_photo(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _import_ml_photo(message, state)


@admin_router.message(AdminFlow.ml_sample_text, F.document)
async def admin_ml_add_document(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _import_ml_file(message, state)


@admin_router.message(AdminFlow.ml_sample_text)
async def admin_ml_add_text(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if len(text) < 15:
        await message.answer(
            "Слишком короткий текст. Пришлите полное объявление, файл или фото с OCR.",
        )
        return
    await state.update_data(ml_sample_text=text)
    preview = text[:400] + ("…" if len(text) > 400 else "")
    await message.answer(
        f"<b>Выберите категорию</b> для примера:\n\n<i>{preview}</i>",
        reply_markup=admin_ml_category_keyboard(),
    )


async def _save_ml_rows(message: Message, state: FSMContext, rows: list[tuple[str, str]], source: str) -> None:
    saved = 0
    skipped = 0
    async with SessionLocal() as session:
        for text, category in rows:
            clean = text.strip()
            if len(clean) < 15:
                skipped += 1
                continue
            session.add(
                TrainingSample(text=clean, target_category=category, source=source),
            )
            saved += 1
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(TrainingSample)) or 0

    await state.clear()
    parts = [f"Добавлено примеров: <b>{saved}</b>"]
    if skipped:
        parts.append(f"Пропущено коротких: <b>{skipped}</b>")
    parts.append(f"Всего в БД: <b>{total}</b>")
    parts.append("\nНажмите <b>Переобучить</b>, чтобы модель учла новые данные.")
    await message.answer("\n".join(parts), reply_markup=admin_retrain_offer_keyboard())


async def _import_ml_photo(message: Message, state: FSMContext) -> None:
    from app.ml.image_ocr import ocr_image_bytes

    photo = message.photo[-1]
    caption = message.caption or ""
    category_hint, text_override, caption_err = parse_photo_caption(caption)

    try:
        tg_file = await message.bot.get_file(photo.file_id)
        buffer = await message.bot.download_file(tg_file.file_path)
        raw = buffer.read()
        ocr_text = (text_override or ocr_image_bytes(raw)).strip()
    except Exception as exc:
        logger.exception("ML photo OCR failed")
        await message.answer(
            f"❌ Не удалось распознать фото:\n<code>{exc}</code>\n\n"
            "Пришлите текст в подписи или отправьте файл-документ.",
        )
        return

    if caption_err and not text_override:
        await message.answer(caption_err)
        return

    if category_hint and len(ocr_text) >= 15:
        await _save_ml_rows(message, state, [(ocr_text, category_hint)], source="admin_photo")
        return

    if len(ocr_text) < 15:
        await message.answer(
            "OCR нашёл мало текста. Добавьте полный текст в подпись:\n"
            "<code>VACANCY</code>\n"
            "<i>полный текст объявления</i>",
        )
        return

    await state.update_data(ml_sample_text=ocr_text)
    preview = ocr_text[:400] + ("…" if len(ocr_text) > 400 else "")
    await message.answer(
        f"<b>Текст распознан с фото</b> — выберите категорию:\n\n<i>{preview}</i>",
        reply_markup=admin_ml_category_keyboard(),
    )


async def _import_ml_file(message: Message, state: FSMContext) -> None:
    doc = message.document
    if not doc:
        await message.answer("Пришлите файл как <b>документ</b> 📄 или фото 📷.")
        return
    max_size = 15_000_000 if (doc.file_name or "").lower().endswith(".zip") else 8_000_000
    if doc.file_size and doc.file_size > max_size:
        await message.answer(f"Файл слишком большой (максимум {max_size // 1_000_000} МБ).")
        return
    filename = doc.file_name or "upload.xlsx"
    try:
        tg_file = await message.bot.get_file(doc.file_id)
        buffer = await message.bot.download_file(tg_file.file_path)
        raw = buffer.read()
        rows = parse_training_file(filename, raw)
    except Exception as exc:
        logger.exception("ML file import failed")
        await message.answer(
            f"❌ Не удалось разобрать файл:\n<code>{exc}</code>\n\n{supported_formats_hint()}",
        )
        return

    if not rows:
        await message.answer(
            "В файле не найдено примеров.\n\n" + supported_formats_hint(),
        )
        return

    await message.answer(f"✅ <b>Файл загружен</b> — <code>{filename}</code>")
    await _save_ml_rows(message, state, rows, source="admin_file")


@admin_router.callback_query(F.data == "adm:ml:file")
async def admin_ml_file_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.ml_sample_file)
    await callback.answer()
    await callback.message.answer(
        "📎 <b>Загрузка файла с примерами</b>\n\n" + supported_formats_hint() + "\n\n"
        "Пришлите <b>документ</b> 📄 или <b>фото</b> 📷 (для одного скрина).",
        reply_markup=admin_back_keyboard(),
    )


@admin_router.message(AdminFlow.ml_sample_file, F.photo)
async def admin_ml_file_photo(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _import_ml_photo(message, state)


@admin_router.message(AdminFlow.ml_sample_file, F.document)
async def admin_ml_file_upload(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _import_ml_file(message, state)


@admin_router.message(AdminFlow.ml_sample_file)
async def admin_ml_file_wrong(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(
        "Нужен файл-документ, фото или архив.\n\n" + supported_formats_hint(),
    )


@admin_router.callback_query(F.data.startswith("adm:ml:cat:"))
async def admin_ml_save_sample(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    category = callback.data.split(":")[-1]
    data = await state.get_data()
    text = data.get("ml_sample_text")
    if not text:
        await callback.answer("Сначала пришлите текст", show_alert=True)
        return
    async with SessionLocal() as session:
        session.add(TrainingSample(text=text, target_category=category, source="admin_add"))
        await session.commit()
        total = await session.scalar(select(func.count()).select_from(TrainingSample)) or 0
    await state.clear()
    cat = CATEGORIES.get(category)
    label = cat.label if cat else category
    await callback.answer("Сохранено")
    await callback.message.answer(
        f"✅ <b>Пример добавлен</b>\n"
        f"Категория: <b>{label}</b>\n"
        f"Всего в БД: <b>{total}</b> примеров\n\n"
        "Нажмите <b>Переобучить</b>, чтобы модель учла новый текст.",
        reply_markup=admin_retrain_offer_keyboard(),
    )


@admin_router.callback_query(F.data == "adm:ml:samples")
async def admin_ml_samples(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        total = await session.scalar(select(func.count()).select_from(TrainingSample)) or 0
        recent = (
            await session.scalars(select(TrainingSample).order_by(desc(TrainingSample.id)).limit(8))
        ).all()
    if not recent:
        await callback.message.answer(
            "📚 Пока нет примеров, добавленных через админку.\n"
            "Используйте <b>➕ Добавить пример</b> или исправляйте логи.",
            reply_markup=admin_ml_keyboard(),
        )
        return
    lines = [f"<b>📚 Примеры в БД</b> — всего <b>{total}</b>\n"]
    for s in recent:
        preview = (s.text[:80] + "…") if len(s.text) > 80 else s.text
        lines.append(f"• <b>{s.target_category}</b> <i>({s.source})</i>\n  {preview}")
    lines.append("\n💡 После добавления — <b>Переобучить модель</b>")
    await callback.message.answer("\n\n".join(lines), reply_markup=admin_ml_keyboard())


@admin_router.callback_query(F.data == "adm:retrain")
async def admin_retrain(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer("Обучение...")
    settings = get_settings()
    try:
        metrics = await train_and_save(Path(settings.training_data_path), Path(settings.ml_model_path))
        get_classifier().reload()
        await callback.message.answer(
            f"✅ Модель переобучена\n"
            f"Примеров: {metrics.get('samples')}\n"
            f"Accuracy: {metrics.get('accuracy', 0):.2%}",
            reply_markup=admin_back_keyboard(),
        )
    except Exception as exc:
        logger.exception("Retrain failed")
        await callback.message.answer(f"❌ {exc}", reply_markup=admin_back_keyboard())


@admin_router.callback_query(F.data == "adm:logs")
async def admin_logs(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    async with SessionLocal() as session:
        logs = (await session.scalars(select(ClassificationLog).order_by(desc(ClassificationLog.id)).limit(5))).all()
    if not logs:
        await callback.message.answer("Логов нет.", reply_markup=admin_back_keyboard())
        await callback.answer()
        return
    for log in logs:
        preview = (log.text[:280] + "…") if len(log.text) > 280 else log.text
        verdict = f" · {log.admin_verdict}" if log.admin_verdict else ""
        await callback.message.answer(
            f"#{log.id} → <b>{log.predicted_category}</b> ({float(log.confidence):.0%}){verdict}\n\n{preview}",
            reply_markup=admin_log_keyboard(log.id),
        )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm:ok:"))
async def admin_log_ok(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    log_id = int(callback.data.split(":")[-1])
    async with SessionLocal() as session:
        log = await session.scalar(select(ClassificationLog).where(ClassificationLog.id == log_id))
        if log:
            log.admin_verdict = "approved"
            log.final_category = log.predicted_category
            session.add(TrainingSample(text=log.text, target_category=log.predicted_category, source="admin_ok"))
            await session.commit()
    await callback.answer("✅ Сохранено")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Пример сохранён для дообучения.\nНажмите <b>Переобучить</b>, когда накопите правки.",
        reply_markup=admin_retrain_offer_keyboard(),
    )


@admin_router.callback_query(F.data.startswith("adm:fix:"))
async def admin_log_fix(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    _, _, log_id, category = callback.data.split(":", 3)
    async with SessionLocal() as session:
        log = await session.scalar(select(ClassificationLog).where(ClassificationLog.id == int(log_id)))
        if log:
            log.admin_verdict = "fixed"
            log.final_category = category
            session.add(TrainingSample(text=log.text, target_category=category, source="admin_fix"))
            await session.commit()
    cat = CATEGORIES.get(category)
    label = cat.label if cat else category
    await callback.answer(f"→ {label}")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"Исправление → <b>{label}</b> сохранено.\nПереобучите модель, чтобы применить.",
        reply_markup=admin_retrain_offer_keyboard(),
    )


# --- Whitelist UI ---

@admin_router.callback_query(F.data == "adm:wl")
async def admin_wl_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    await callback.message.answer(
        "<b>👥 Whitelist</b>\nБесплатная подписка для выбранных пользователей.",
        reply_markup=admin_wl_keyboard(),
    )


@admin_router.callback_query(F.data == "adm:wl:list")
async def admin_wl_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = (await session.scalars(select(WhitelistEntry).order_by(WhitelistEntry.id))).all()
    if not rows:
        await callback.message.answer("Whitelist пуст.", reply_markup=admin_wl_keyboard())
        return
    lines = ["<b>👥 Whitelist</b>\n"]
    del_rows: list[list] = []
    for row in rows:
        keys = parse_city_keys(row.city_keys)
        scope = "все города" if not keys else ", ".join(sorted(keys))
        chats = parse_chat_ids(row.chat_ids)
        chat_scope = f" · чаты: {len(chats)}" if chats else ""
        note = f" — {row.comment}" if row.comment else ""
        uname = f" @{row.username}" if row.username else ""
        if row.telegram_id:
            who = f"<code>{row.telegram_id}</code>{uname}"
        else:
            who = f"<b>@{row.username}</b> <i>(ожидает /start)</i>" if row.username else "—"
        lines.append(f"• #{row.id} {who} → {scope}{chat_scope}{note}")
        del_rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 #{row.id}",
                    callback_data=f"adm:wl:del:{row.id}",
                    style=ButtonStyle.DANGER,
                ),
            ],
        )
    # Пакуем кнопки удаления по 3 в ряд
    packed: list[list] = []
    buf: list = []
    for btn_row in del_rows:
        buf.extend(btn_row)
        if len(buf) >= 3:
            packed.append(buf)
            buf = []
    if buf:
        packed.append(buf)
    packed.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:wl", style=ButtonStyle.PRIMARY)])
    kb = InlineKeyboardMarkup(inline_keyboard=packed)
    await callback.message.answer("\n".join(lines), reply_markup=kb)


@admin_router.callback_query(F.data.startswith("adm:wl:del:"))
async def admin_wl_del_cb(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    entry_id = int(callback.data.split(":")[-1])
    label = ""
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.id == entry_id))
        if row:
            if row.telegram_id:
                label = str(row.telegram_id)
            elif row.username:
                label = f"@{row.username}"
            else:
                label = f"#{row.id}"
            await session.delete(row)
            await session.commit()
    await callback.answer("Удалён")
    await callback.message.answer(f"🗑 Удалён из whitelist: <code>{label}</code>", reply_markup=admin_wl_keyboard())


@admin_router.callback_query(F.data == "adm:wl:add")
async def admin_wl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.whitelist_username)
    await state.update_data(wl_selected_cities=[], wl_selected_chats=[])
    await callback.answer()
    await callback.message.answer(
        "➕ <b>Добавить в whitelist</b>\n\n"
        "Отправьте <b>@username</b> или числовой Telegram ID.\n\n"
        "<i>Если пользователь ещё не писал боту — можно добавить только @username, "
        "доступ включится после его /start.</i>",
        reply_markup=admin_back_keyboard(),
    )


@admin_router.message(AdminFlow.whitelist_username)
async def admin_wl_username(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    if message.text in ("⬅️ В админку", "◀️ В админку") or (message.text and message.text.startswith("/admin")):
        await state.clear()
        if message.text and message.text.startswith("/admin"):
            await _show_panel(message)
        return

    raw = (message.text or "").strip()
    from app.bot.runtime import get_bot
    from app.services.telegram_resolve import resolve_telegram_user

    resolved = await resolve_telegram_user(get_bot(), raw)
    if not resolved.telegram_id and not resolved.username:
        await message.answer("Введите <b>@username</b> или числовой Telegram ID")
        return

    tg_id = resolved.telegram_id
    username = resolved.username
    pending = resolved.pending_username

    # getChat часто не резолвит @username — ищем в нашей таблице users
    if pending and username and not tg_id:
        async with SessionLocal() as session:
            known = await session.scalar(
                select(User).where(func.lower(User.username) == username.lower()),
            )
            if known:
                tg_id = known.telegram_id
                pending = False
                username = (known.username or username).lstrip("@").lower()

    if pending and username:
        async with SessionLocal() as session:
            exists = await session.scalar(
                select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == username.lower()),
            )
            if exists:
                await message.answer(f"@{username} уже в whitelist.")
                return

    if tg_id and not pending:
        async with SessionLocal() as session:
            exists = await session.scalar(
                select(WhitelistEntry).where(WhitelistEntry.telegram_id == int(tg_id)),
            )
            if exists:
                await message.answer(f"ID <code>{tg_id}</code> уже в whitelist.")
                return

    await state.update_data(wl_tg_id=tg_id, wl_username=username, wl_pending=pending)
    async with SessionLocal() as session:
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if not cities:
        await message.answer("Нет активных городов.")
        return
    await state.set_state(AdminFlow.whitelist_cities)
    kb = _wl_cities_keyboard(cities, set())
    if pending:
        user_line = f"Пользователь: <b>@{username}</b> (ожидает /start в боте)"
    else:
        user_line = f"Пользователь: <code>{tg_id}</code>" + (f" (@{username})" if username else "")
    await message.answer(
        f"{user_line}\n\nВыберите города (можно несколько) или <b>Все города</b>:",
        reply_markup=kb,
    )


def _wl_cities_keyboard(cities: list[City], selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for city in cities:
        mark = "✅" if city.key in selected else "🗺"
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} {city.label}",
                callback_data=f"adm:wl:city:{city.key}",
                style=ButtonStyle.SUCCESS if city.key in selected else ButtonStyle.PRIMARY,
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="🌍 Все города", callback_data="adm:wl:city:all", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="✅ Сохранить", callback_data="adm:wl:city:save", style=ButtonStyle.SUCCESS),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:wl", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _wl_chats_keyboard(chats: list[Chat], selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for chat in chats:
        mark = "✅" if chat.id in selected else "💬"
        topic = f" · {chat.topic}" if chat.topic else ""
        rows.append([
            InlineKeyboardButton(
                text=f"{mark} @{chat.telegram_username}{topic}",
                callback_data=f"adm:wl:chat:{chat.id}",
                style=ButtonStyle.SUCCESS if chat.id in selected else ButtonStyle.PRIMARY,
            ),
        ])
    rows.append([
        InlineKeyboardButton(text="🌍 Все чаты", callback_data="adm:wl:chat:all", style=ButtonStyle.SUCCESS),
        InlineKeyboardButton(text="✅ Готово", callback_data="adm:wl:chat:save", style=ButtonStyle.SUCCESS),
    ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:wl", style=ButtonStyle.PRIMARY)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _wl_chats_for_state(session, data: dict) -> list[Chat]:
    city_keys = data.get("wl_selected_cities") or []
    cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    if city_keys:
        city_ids = [c.id for c in cities if c.key in city_keys]
    else:
        city_ids = [c.id for c in cities]
    if not city_ids:
        return []
    return list(
        await session.scalars(
            select(Chat).where(Chat.city_id.in_(city_ids), Chat.active.is_(True)).order_by(Chat.sort_order),
        ),
    )


@admin_router.callback_query(AdminFlow.whitelist_cities, F.data.startswith("adm:wl:city:"))
async def admin_wl_toggle_city(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    key = callback.data.split(":")[-1]
    data = await state.get_data()
    if key == "save":
        await callback.answer()
        data = await state.get_data()
        city_keys = data.get("wl_selected_cities") or []
        async with SessionLocal() as session:
            cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
            if city_keys:
                city_ids = [c.id for c in cities if c.key in city_keys]
            else:
                city_ids = [c.id for c in cities]
            if not city_ids:
                await callback.message.answer("Нет городов для выбора чатов.")
                return
            chats = (
                await session.scalars(
                    select(Chat)
                    .where(Chat.city_id.in_(city_ids), Chat.active.is_(True))
                    .order_by(Chat.sort_order),
                )
            ).all()
        if not chats:
            await state.set_state(AdminFlow.whitelist_comment)
            await callback.message.answer(
                "В выбранных городах нет чатов.\nКомментарий (или <code>-</code> чтобы пропустить):",
            )
            return
        await state.set_state(AdminFlow.whitelist_chats)
        await state.update_data(wl_selected_chats=[])
        await callback.message.edit_text(
            "<b>💬 Whitelist — выбор чатов</b>\n\n"
            "Отметьте разрешённые чаты или нажмите <b>Все чаты</b>.\n"
            "Пустой выбор = все чаты в выбранных городах.",
            reply_markup=_wl_chats_keyboard(chats, set()),
        )
        return
    selected = set(data.get("wl_selected_cities") or [])
    if key == "all":
        selected = set()
        await state.update_data(wl_selected_cities=[])
    else:
        if key in selected:
            selected.remove(key)
        else:
            selected.add(key)
        await state.update_data(wl_selected_cities=list(selected))
    async with SessionLocal() as session:
        cities = (await session.scalars(select(City).where(City.active.is_(True)))).all()
    await callback.message.edit_reply_markup(reply_markup=_wl_cities_keyboard(cities, selected))
    await callback.answer()


@admin_router.callback_query(AdminFlow.whitelist_chats, F.data.startswith("adm:wl:chat:"))
async def admin_wl_toggle_chat(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    token = callback.data.split(":")[-1]
    data = await state.get_data()
    if token == "save":
        await state.set_state(AdminFlow.whitelist_comment)
        await callback.answer()
        await callback.message.answer("Комментарий (или <code>-</code> чтобы пропустить):")
        return
    selected = set(data.get("wl_selected_chats") or [])
    if token == "all":
        selected = set()
        await state.update_data(wl_selected_chats=[])
    else:
        chat_id = int(token)
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.add(chat_id)
        await state.update_data(wl_selected_chats=list(selected))
    async with SessionLocal() as session:
        chats = await _wl_chats_for_state(session, data)
    await callback.message.edit_reply_markup(reply_markup=_wl_chats_keyboard(chats, selected))
    await callback.answer()


@admin_router.message(AdminFlow.whitelist_comment)
async def admin_wl_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    data = await state.get_data()
    comment = (message.text or "").strip()
    if comment == "-":
        comment = None
    tg_id = data.get("wl_tg_id")
    pending = bool(data.get("wl_pending"))
    cities = data.get("wl_selected_cities") or []
    chat_ids = data.get("wl_selected_chats") or []
    username = (data.get("wl_username") or "").strip().lstrip("@") or None
    if username:
        username = username.lower()
    sub_note = ""
    bound_tg = int(tg_id) if tg_id else None
    async with SessionLocal() as session:
        from app.services.whitelist import try_bind_entry_from_existing_user

        if pending or not tg_id:
            row = None
            if username:
                row = await session.scalar(
                    select(WhitelistEntry).where(func.lower(WhitelistEntry.username) == username.lower()),
                )
        else:
            row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == int(tg_id)))
        if row:
            row.city_keys = json.dumps(cities)
            row.chat_ids = json.dumps(chat_ids)
            row.username = username or row.username
            row.comment = comment
            if tg_id and not row.telegram_id:
                row.telegram_id = int(tg_id)
        else:
            row = WhitelistEntry(
                telegram_id=int(tg_id) if tg_id else None,
                username=username,
                city_keys=json.dumps(cities),
                chat_ids=json.dumps(chat_ids),
                comment=comment,
            )
            session.add(row)
        await session.commit()
        await session.refresh(row)

        # Если юзер уже писал боту — привязать telegram_id сразу (getChat часто не резолвит)
        row = await try_bind_entry_from_existing_user(session, row)
        if row and row.telegram_id:
            bound_tg = int(row.telegram_id)

        if row and row.telegram_id:
            ok, reason, sub_id = await provision_whitelist_subscription(session, row)
            if ok and sub_id:
                from app.services.notifications import notify_payment_success

                user = await session.scalar(select(User).where(User.telegram_id == row.telegram_id))
                if user and reason != "synced":
                    await notify_payment_success(user.id, "subscription_whitelist", sub_id)
                sub_note = f"\n📋 Подписка <b>#{sub_id}</b> активирована автоматически."
            elif reason == "no_chats":
                sub_note = "\n⚠️ Подписка не создана: нет активных чатов по выбранному охвату."
            elif reason == "user_not_started":
                sub_note = "\n⚠️ Пользователь ещё не писал боту — подписка создастся после /start."
            elif reason == "pending_user":
                sub_note = "\n⏳ Подписка включится после <b>/start</b> пользователя."
        elif row and not row.telegram_id:
            sub_note = (
                "\n⏳ Пользователь ещё не найден в боте — "
                "подписка включится после его <b>/start</b>."
            )

    await state.clear()
    scope = "все города" if not cities else ", ".join(cities)
    chat_scope = f" · чаты: {len(chat_ids)}" if chat_ids else " · все чаты"
    if bound_tg and username:
        who = f"<code>{bound_tg}</code> @{username}"
    elif bound_tg:
        who = f"<code>{bound_tg}</code>"
    elif username:
        who = f"@{username}"
    else:
        who = "пользователь"
    await message.answer(
        f"✅ Whitelist: {who} → {scope}{chat_scope}{sub_note}",
        reply_markup=admin_wl_keyboard(),
    )


# --- WhiteLabel applications ---


@admin_router.callback_query(F.data == "adm:wlreq")
async def admin_wlreq_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    pending = await _wl_pending_count()
    await callback.message.answer(
        f"<b>📝 Заявки WhiteLabel</b>\n"
        f"Ожидают рассмотрения: <b>{pending}</b>",
        reply_markup=admin_wlreq_keyboard(),
    )


@admin_router.callback_query(F.data.startswith("adm:wlreq:list:"))
async def admin_wlreq_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    mode = callback.data.rsplit(":", 1)[-1]
    async with SessionLocal() as session:
        query = select(WhitelabelApplication).order_by(WhitelabelApplication.id.desc()).limit(20)
        if mode == "pending":
            query = query.where(WhitelabelApplication.status == WhitelabelApplicationStatus.pending)
        rows = (await session.scalars(query)).all()
    if not rows:
        await callback.message.answer("Заявок нет.", reply_markup=admin_wlreq_keyboard())
        return
    lines = ["<b>📝 Заявки WhiteLabel</b>\n"]
    kb_rows = []
    for row in rows[:10]:
        mark = {"pending": "⏳", "approved": "✅", "rejected": "❌"}.get(row.status.value, "•")
        uname = f"@{row.username}" if row.username else str(row.telegram_id)
        lines.append(f"{mark} <b>#{row.id}</b> {row.brand_title} — {uname}")
        kb_rows.append([
            InlineKeyboardButton(
                text=f"#{row.id} {row.brand_title[:20]}",
                callback_data=f"adm:wlreq:view:{row.id}",
                style=ButtonStyle.PRIMARY,
            ),
        ])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:wlreq", style=ButtonStyle.PRIMARY)])
    await callback.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@admin_router.callback_query(F.data.startswith("adm:wlreq:view:"))
async def admin_wlreq_view(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
    if not app_row:
        await callback.answer("Не найдена", show_alert=True)
        return
    await callback.answer()
    can_partner = app_row.status != WhitelabelApplicationStatus.rejected
    await callback.message.answer(
        _format_wl_application(app_row),
        reply_markup=admin_wlreq_view_keyboard(app_id, can_partner=can_partner),
    )


@admin_router.callback_query(F.data.startswith("adm:wlreq:approve:"))
async def admin_wlreq_approve(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        app_row.status = WhitelabelApplicationStatus.approved
        app_row.processed_at = datetime.now(UTC)
        tg_id = app_row.telegram_id
        await session.commit()
    await callback.answer("Одобрено")
    from app.services.notifications import notify_user_wl_application_status

    await notify_user_wl_application_status(tg_id, app_id, approved=True)
    await callback.message.answer(
        f"✅ Заявка #{app_id} одобрена, пользователь уведомлён.",
        reply_markup=admin_wlreq_keyboard(),
    )


@admin_router.callback_query(F.data.startswith("adm:wlreq:reject:"))
async def admin_wlreq_reject(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        app_row.status = WhitelabelApplicationStatus.rejected
        app_row.processed_at = datetime.now(UTC)
        tg_id = app_row.telegram_id
        await session.commit()
    await callback.answer("Отклонено")
    from app.services.notifications import notify_user_wl_application_status

    await notify_user_wl_application_status(tg_id, app_id, approved=False)
    await callback.message.answer(
        f"❌ Заявка #{app_id} отклонена, пользователь уведомлён.",
        reply_markup=admin_wlreq_keyboard(),
    )


@admin_router.callback_query(F.data.startswith("adm:wlreq:partner:"))
async def admin_wlreq_create_partner(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    app_id = int(callback.data.rsplit(":", 1)[-1])
    from datetime import UTC, datetime

    async with SessionLocal() as session:
        app_row = await session.scalar(select(WhitelabelApplication).where(WhitelabelApplication.id == app_id))
        if not app_row:
            await callback.answer("Не найдена", show_alert=True)
            return
        if app_row.status == WhitelabelApplicationStatus.pending:
            app_row.status = WhitelabelApplicationStatus.approved
            app_row.processed_at = datetime.now(UTC)
            await session.commit()
            from app.services.notifications import notify_user_wl_application_status

            await notify_user_wl_application_status(app_row.telegram_id, app_id, approved=True)
        bot_username = app_row.planned_bot_username
        owner_id = app_row.telegram_id
        brand = app_row.brand_title

    await callback.answer()
    await state.update_data(wlbl_owner_id=owner_id, wlbl_brand=brand, wlbl_from_app=app_id)
    if bot_username:
        await state.update_data(wlbl_bot=bot_username)
        await state.set_state(AdminFlow.wlbl_token)
        await callback.message.answer(
            f"🤖 Создание партнёра по заявке <b>#{app_id}</b>\n"
            f"Бот: @{bot_username} · owner <code>{owner_id}</code>\n\n"
            "Отправьте токен бота от BotFather (или <code>-</code>):",
            reply_markup=admin_back_keyboard(),
        )
    else:
        await state.set_state(AdminFlow.wlbl_bot_username)
        await callback.message.answer(
            f"🤖 Создание партнёра по заявке <b>#{app_id}</b>\n"
            f"Owner: <code>{owner_id}</code> · бренд: <b>{brand}</b>\n\n"
            "Отправьте @username бота-клона:",
            reply_markup=admin_back_keyboard(),
        )


# --- WhiteLabel partners ---


@admin_router.callback_query(F.data == "adm:wlbl")
async def admin_wlbl_menu(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    pending = await _wl_pending_count()
    await callback.message.answer(
        "<b>🏷 WhiteLabel</b>\n"
        "Реестр партнёрских ботов-клонов с автозапуском polling.\n"
        "API: <code>/api/ml/classify</code>, <code>/api/wl/publish</code>\n"
        "Панель владельца: <code>/franchise</code>",
        reply_markup=admin_wlbl_keyboard(pending),
    )


@admin_router.callback_query(F.data == "adm:wlbl:list")
async def admin_wlbl_list(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer()
    async with SessionLocal() as session:
        rows = (await session.scalars(select(WhitelabelPartner).order_by(WhitelabelPartner.id))).all()
    if not rows:
        await callback.message.answer("Партнёров пока нет.", reply_markup=admin_wlbl_keyboard())
        return
    lines = ["<b>🏷 WhiteLabel боты</b>\n"]
    for row in rows:
        status = "✅" if row.active else "⛔"
        owner = f" · owner {row.owner_telegram_id}" if row.owner_telegram_id else ""
        note = f" — {row.note}" if row.note else ""
        token = " · 🤖 polling" if row.bot_token else " · API only"
        brand = f" · {row.brand_title}" if row.brand_title else ""
        lines.append(
            f"{status} @{row.bot_username}{brand}{owner}{token}\n"
            f"   key: <code>{row.api_key}</code>{note}",
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'⛔' if rows[-1].active else '✅'} @{rows[-1].bot_username}",
                    callback_data=f"adm:wlbl:toggle:{rows[-1].id}",
                    style=ButtonStyle.DANGER if rows[-1].active else ButtonStyle.SUCCESS,
                ),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:wlbl", style=ButtonStyle.PRIMARY)],
        ],
    )
    await callback.message.answer("\n".join(lines), reply_markup=kb)


@admin_router.callback_query(F.data.startswith("adm:wlbl:toggle:"))
async def admin_wlbl_toggle(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    partner_id = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.id == partner_id))
        if row:
            row.active = not row.active
            await session.commit()
            from app.bot.runtime import init_partner_bots

            await init_partner_bots()
            status = "активен" if row.active else "отключён"
            await callback.answer(status)
            await callback.message.answer(f"@{row.bot_username} — {status}", reply_markup=admin_wlbl_keyboard())
            return
    await callback.answer("Не найден", show_alert=True)


@admin_router.callback_query(F.data == "adm:wlbl:add")
async def admin_wlbl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await state.set_state(AdminFlow.wlbl_bot_username)
    await callback.answer()
    await callback.message.answer(
        "➕ <b>WhiteLabel бот</b>\n\n"
        "Отправьте @username бота-клона:",
        reply_markup=admin_back_keyboard(),
    )


@admin_router.message(AdminFlow.wlbl_bot_username)
async def admin_wlbl_bot_username(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip().lstrip("@")
    if not raw:
        await message.answer("Укажите @username бота.")
        return
    await state.update_data(wlbl_bot=raw)
    await state.set_state(AdminFlow.wlbl_owner)
    await message.answer("Telegram ID владельца (или <code>-</code>):")


@admin_router.message(AdminFlow.wlbl_owner)
async def admin_wlbl_owner(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    owner_id = None
    if raw and raw != "-":
        try:
            owner_id = int(raw)
        except ValueError:
            await message.answer("Введите числовой ID или <code>-</code>")
            return
    await state.update_data(wlbl_owner_id=owner_id)
    await state.set_state(AdminFlow.wlbl_token)
    await message.answer(
        "Токен бота-клона от BotFather (или <code>-</code> — только API без polling):",
    )


@admin_router.message(AdminFlow.wlbl_token)
async def admin_wlbl_token(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    token = None if raw in {"", "-"} else raw
    await state.update_data(wlbl_token=token)
    data = await state.get_data()
    if data.get("wlbl_brand"):
        await state.set_state(AdminFlow.wlbl_note)
        await message.answer(
            f"Бренд: <b>{data['wlbl_brand']}</b>\n"
            "Комментарий (или <code>-</code>):",
        )
        return
    await state.set_state(AdminFlow.wlbl_brand)
    await message.answer("Отображаемое имя бота (BotFather / setMyName), или <code>-</code>:")


@admin_router.message(AdminFlow.wlbl_brand)
async def admin_wlbl_brand(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    data = await state.get_data()
    if raw in {"", "-"} and data.get("wlbl_brand"):
        brand = data["wlbl_brand"]
    else:
        brand = None if raw in {"", "-"} else raw
    await state.update_data(wlbl_brand=brand)
    await state.set_state(AdminFlow.wlbl_note)
    await message.answer("Комментарий (или <code>-</code>):")


@admin_router.message(AdminFlow.wlbl_note)
async def admin_wlbl_save(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        return
    import secrets

    from app.bot.runtime import init_partner_bots

    data = await state.get_data()
    note = (message.text or "").strip()
    if note == "-":
        note = None
    bot_username = data["wlbl_bot"]
    owner_id = data.get("wlbl_owner_id")
    bot_token = data.get("wlbl_token")
    brand_title = data.get("wlbl_brand")
    api_key = secrets.token_urlsafe(24)
    partner_id: int | None = None
    async with SessionLocal() as session:
        exists = await session.scalar(select(WhitelabelPartner).where(WhitelabelPartner.bot_username == bot_username))
        if exists:
            exists.owner_telegram_id = owner_id
            exists.note = note
            exists.bot_token = bot_token
            exists.brand_title = brand_title
            exists.active = True
            api_key = exists.api_key
            partner_id = exists.id
        else:
            row = WhitelabelPartner(
                bot_username=bot_username,
                owner_telegram_id=owner_id,
                api_key=api_key,
                bot_token=bot_token,
                brand_title=brand_title,
                note=note,
            )
            session.add(row)
            await session.flush()
            partner_id = row.id
        await session.commit()
    await state.clear()
    await init_partner_bots()
    token_line = "✅ polling" if bot_token else "⏳ без токена"
    await message.answer(
        f"✅ WhiteLabel: <b>@{bot_username}</b> ({token_line})\n\n"
        f"API key:\n<code>{api_key}</code>\n\n"
        "Endpoints:\n"
        "<code>POST /api/ml/classify</code>\n"
        "<code>POST /api/wl/publish</code>\n"
        "Header: <code>X-API-Key</code>\n\n"
        "Владелец: команда <code>/franchise</code>",
        reply_markup=admin_wlbl_keyboard(),
    )


# --- Chats ---

@admin_router.callback_query(F.data == "adm:chats")
async def admin_check_chats(callback: CallbackQuery) -> None:
    if not _is_admin(callback.from_user.id):
        return
    await callback.answer("Проверяю...")
    from app.bot.runtime import get_bot

    bot = get_bot()
    async with SessionLocal() as session:
        chats = (await session.scalars(select(Chat).where(Chat.active.is_(True)).order_by(Chat.sort_order))).all()
        me = await bot.get_me()
        ok_lines: list[str] = []
        bad_lines: list[str] = []
        fixed = 0
        for chat in chats:
            label = f"@{chat.telegram_username}"
            chat_id = chat.telegram_chat_id
            if not chat_id:
                try:
                    tg = await bot.get_chat(f"@{chat.telegram_username}")
                    chat.telegram_chat_id = tg.id
                    fixed += 1
                    chat_id = tg.id
                except Exception as exc:
                    bad_lines.append(f"❌ {label} — нет id: {str(exc)[:50]}")
                    continue
            try:
                tg_chat = await bot.get_chat(chat_id)
                member = await bot.get_chat_member(chat_id, me.id)
                if member.status in {"administrator", "creator", "member"}:
                    pin = "📌" if member.status == "administrator" else ""
                    ok_lines.append(f"✅ {label} {pin}")
                else:
                    bad_lines.append(f"⚠️ {label} — {member.status}")
            except Exception as exc:
                bad_lines.append(f"❌ {label} — {str(exc)[:60]}")
        if fixed:
            await session.commit()

    text = "<b>💬 Чаты</b>\n╭──────────────────────╮\n\n"
    if fixed:
        text += f"<i>Обновлено chat_id: {fixed}</i>\n\n"
    text += "\n".join(ok_lines[:25]) if ok_lines else "—"
    if bad_lines:
        text += "\n\n<b>Проблемы:</b>\n" + "\n".join(bad_lines[:15])
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="adm:chats", style=ButtonStyle.PRIMARY)],
            [InlineKeyboardButton(text="⬅️ В админку", callback_data="adm:home", style=ButtonStyle.PRIMARY)],
        ],
    )
    await callback.message.answer(text, reply_markup=kb)


# --- Legacy commands ---

@admin_router.message(Command("whitelist_add"))
async def whitelist_add(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 3:
        await message.answer(
            "Формат: /whitelist_add <telegram_id> <city_keys> [chat_ids] [comment]\n"
            "city_keys: msk,spb или all\n"
            "chat_ids: 1,2,3 или -\n"
            "Или /admin → Whitelist",
        )
        return
    tg_id = int(parts[1])
    raw_cities = parts[2].strip().lower()
    cities: list[str] = [] if raw_cities in {"all", "*", "все"} else [x.strip().lower() for x in parts[2].split(",") if x.strip()]
    chat_ids: list[int] = []
    comment = None
    if len(parts) > 3:
        raw_chats = parts[3].strip()
        if raw_chats not in {"-", ""}:
            for item in raw_chats.split(","):
                item = item.strip()
                if item:
                    chat_ids.append(int(item))
        comment = parts[4] if len(parts) > 4 else None
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == tg_id))
        if row:
            row.city_keys = json.dumps(cities)
            row.chat_ids = json.dumps(chat_ids)
            row.comment = comment
        else:
            row = WhitelistEntry(
                telegram_id=tg_id,
                city_keys=json.dumps(cities),
                chat_ids=json.dumps(chat_ids),
                comment=comment,
            )
            session.add(row)
        await session.commit()
        sub_note = ""
        ok, reason, sub_id = await provision_whitelist_subscription(session, row)
        if ok and sub_id:
            from app.services.notifications import notify_payment_success

            user = await session.scalar(select(User).where(User.telegram_id == tg_id))
            if user:
                await notify_payment_success(user.id, "subscription_whitelist", sub_id)
            sub_note = f"\n📋 Подписка <b>#{sub_id}</b> активирована автоматически."
        elif reason == "no_chats":
            sub_note = "\n⚠️ Подписка не создана: нет активных чатов."
        elif reason == "user_not_started":
            sub_note = "\n⚠️ Пользователь ещё не писал боту — подписка создастся после /start."
    scope = "все города" if not cities else ", ".join(cities)
    chats_scope = "все чаты" if not chat_ids else ", ".join(str(x) for x in chat_ids)
    await message.answer(f"✅ Whitelist: <code>{tg_id}</code> → {scope} · чаты: {chats_scope}{sub_note}")


@admin_router.message(Command("whitelist_del"))
async def whitelist_del(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Формат: /whitelist_del <telegram_id>")
        return
    tg_id = int(parts[1])
    async with SessionLocal() as session:
        row = await session.scalar(select(WhitelistEntry).where(WhitelistEntry.telegram_id == tg_id))
        if row:
            await session.delete(row)
            await session.commit()
    await message.answer(f"🗑 Удалён: <code>{tg_id}</code>")
