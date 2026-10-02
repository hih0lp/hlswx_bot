"""«Управление» и проверка категорий (ТЗ этапа 2, 6.11).

Кадры макета: `349:311` — экран «Управление», `349:354` — раздел проверки,
`349:366` — карточка объявления, `349:381` — выбор категории, `349:404` —
категория сохранена.

Раздел называется в макете «Обучение модели» и управляет обучением целиком:
проверка вердикта, пополнение выборки руками и файлом, переобучение по кнопке.
По ТЗ 4.2 в объём этапа не входит создание новой модели — дообучается та же,
что уже работает в боте, антифроде и публичном API.

Вердикт администратора тоже пополняет обучающую выборку: так написано
на кадре `349:404` («Пример добавлен для обучения модели»).
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import desc, func, select

from app.bot.filters import IsAdmin, IsPlatformBot
from app.config import get_settings
from app.core import admin_texts as T
from app.db.session import SessionLocal
from app.keyboards.admin import admin_log_keyboard, admin_ml_category_keyboard
from app.keyboards.pagination import page_slice, pager_row
from app.keyboards.style import STYLE_MAIN, STYLE_PLAIN
from app.ml.classifier import get_classifier
from app.ml.import_samples import (
    parse_photo_caption,
    parse_training_file,
    supported_formats_hint,
)
from app.ml.train import train_and_save
from app.models.entities import ClassificationLog, TrainingSample
from app.services import admin_ui, tariffs
from app.states.admin import AdminFlow

logger = logging.getLogger("admin")
router = Router()
# Раздел «Управление» целиком доступен только администратору (ТЗ 6.11, 7).
router.callback_query.filter(IsPlatformBot(), IsAdmin())
router.message.filter(IsPlatformBot(), IsAdmin())

MANAGE_CB = "adm:manage"
REVIEW_CB = "adm:ml"                 # префикс сохранён: старые кнопки в истории чатов
PENDING_CB = "adm:ml:pending:"       # + страница
CARD_CB = "adm:ml:card:"             # + id лога
PICK_CB = "adm:ml:pick:"             # + id лога
SAMPLES_CB = "adm:ml:samples"
SAMPLES_PAGE_CB = "adm:ml:spage:"    # + страница
ADD_CB = "adm:ml:add"
FILE_CB = "adm:ml:file"
CATEGORY_CB = "adm:ml:cat:"          # + код категории
RETRAIN_CB = "adm:retrain"           # префикс этапа 1 — кнопки живы в истории
LOGS_CB = "adm:logs"
LOGS_PAGE_CB = "adm:logs:page:"      # + страница
LOG_CARD_CB = "adm:logs:card:"       # + id лога

# Экран уходит подписью к баннеру, а подпись в Telegram ограничена 1024
# символами (см. services/banners.py). Списки живут кнопками, поэтому в текст
# попадает только счётчик; в карточке обрезаем сам текст объявления.
PENDING_PER_PAGE = 8
SAMPLES_PER_PAGE = 6
LOGS_PER_PAGE = 8
CARD_TEXT_LIMIT = 600
BUTTON_PREVIEW = 20
SAMPLE_PREVIEW = 60
# Объявление короче этого — обрывок, на котором модели учиться нечему.
MIN_SAMPLE_LEN = 15
# Лимиты Telegram на скачивание: архив тяжелее остальных форматов.
MAX_ZIP_SIZE = 15_000_000
MAX_FILE_SIZE = 8_000_000


@router.callback_query(F.data == MANAGE_CB)
async def admin_manage(callback: CallbackQuery, state: FSMContext) -> None:
    """Экран «Управление» — кадр макета 349:311."""
    await state.clear()
    await callback.answer()
    # На кадре кнопки стоят парами (2+2+1). Первая пара помещается — «Белый
    # список» 15 знакомест, «Сообщения» 12 при пределе 16 на половину ряда, —
    # и её собираем как в макете. Вторую пару собрать нельзя: «🧠 Обучение
    # модели» это 18 знакомест, хвост обрежется. По правилу заказчика «не
    # влезает в кнопку — правим макет» эти две остаются каждая своим рядом,
    # а кадр ждёт правки (docs/figma_fixes_pending.md).
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                # Ведём на `adm:manage:wl`, а не на `adm:wl`: экран тот же, но
                # у пути из «Управления» своя ветка «Найти пользователя» и свой
                # возврат — так нарисовано на кадре 349:522.
                InlineKeyboardButton(text=T.BTN_MANAGE_WHITELIST, callback_data="adm:manage:wl", style=STYLE_PLAIN),
                InlineKeyboardButton(text=T.BTN_MANAGE_MESSAGES, callback_data="adm:msg", style=STYLE_PLAIN),
            ],
            [InlineKeyboardButton(text=T.BTN_MANAGE_ML, callback_data=REVIEW_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_MANAGE_ACCESS, callback_data="adm:access", style=STYLE_PLAIN)],
            [admin_ui.panel_button()],
        ],
    )
    await admin_ui.show(callback, T.MANAGE, markup)


# --------------------------------------------------------- 6.11 проверка категорий


async def pending_count() -> int:
    """Сколько объявлений ждёт вердикта администратора (кадр 349:354)."""
    async with SessionLocal() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(ClassificationLog)
                .where(ClassificationLog.admin_verdict.is_(None)),
            )
            or 0,
        )


async def samples_count() -> int:
    """Сколько примеров накоплено сверх исходной выборки заказчика."""
    async with SessionLocal() as session:
        return int(await session.scalar(select(func.count()).select_from(TrainingSample)) or 0)


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORIES_PENDING,
                    callback_data=f"{PENDING_CB}0",
                    style=STYLE_PLAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORIES_SAMPLES,
                    callback_data=SAMPLES_CB,
                    style=STYLE_PLAIN,
                ),
            ],
            [InlineKeyboardButton(text=T.BTN_CATEGORIES_ADD, callback_data=ADD_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_CATEGORIES_FILE, callback_data=FILE_CB, style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_CATEGORIES_LOGS, callback_data=f"{LOGS_PAGE_CB}0", style=STYLE_PLAIN)],
            [InlineKeyboardButton(text=T.BTN_CATEGORIES_RETRAIN, callback_data=RETRAIN_CB, style=STYLE_MAIN)],
            [admin_ui.back_button(MANAGE_CB, T.BTN_BACK_TO_MANAGE)],
        ],
    )


async def _review_screen() -> tuple[str, InlineKeyboardMarkup]:
    """Раздел «Обучение модели» — кадр макета 349:354 плюс счётчики выборки."""
    text = T.CATEGORIES_REVIEW.format(
        pending=await pending_count(),
        trained=get_classifier().meta.get("samples", "—"),
        added=await samples_count(),
    )
    return text, _review_keyboard()


@router.callback_query(F.data == REVIEW_CB)
async def admin_review(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer()
    await admin_ui.show(callback, *await _review_screen())


def _preview(text: str, limit: int) -> str:
    clean = " ".join((text or "").split())
    return clean[:limit] + "…" if len(clean) > limit else clean


@router.callback_query(F.data.startswith(PENDING_CB))
async def admin_review_pending(callback: CallbackQuery) -> None:
    """Список объявлений без вердикта с круговой пагинацией."""
    await callback.answer()
    page = int(callback.data.rsplit(":", 1)[-1])
    async with SessionLocal() as session:
        rows = list(
            (
                await session.scalars(
                    select(ClassificationLog)
                    .where(ClassificationLog.admin_verdict.is_(None))
                    .order_by(desc(ClassificationLog.id)),
                )
            ).all(),
        )

    shown, page, pages = page_slice(rows, page, PENDING_PER_PAGE)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{log.id} · {_preview(log.text, BUTTON_PREVIEW)}",
                callback_data=f"{CARD_CB}{log.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for log in shown
    ]
    pager = pager_row(PENDING_CB, page, pages)
    if pager:
        buttons.append(pager)
    buttons.append([admin_ui.back_button(REVIEW_CB)])

    text = T.CATEGORIES_PENDING.format(count=len(rows)) if rows else T.CATEGORIES_PENDING_EMPTY
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))


async def _log(log_id: int) -> ClassificationLog | None:
    async with SessionLocal() as session:
        return await session.scalar(select(ClassificationLog).where(ClassificationLog.id == log_id))


def _card(log: ClassificationLog, back_cb: str = f"{PENDING_CB}0") -> tuple[str, InlineKeyboardMarkup]:
    """Карточка объявления — кадр макета 349:366."""
    text = T.CATEGORY_CARD.format(
        number=log.id,
        text=_preview(log.text, CARD_TEXT_LIMIT),
        category=tariffs.get(log.predicted_category).label,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORY_CONFIRM,
                    callback_data=f"adm:ok:{log.id}",
                    style=STYLE_MAIN,
                ),
            ],
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORY_CHANGE,
                    callback_data=f"{PICK_CB}{log.id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button(back_cb)],
        ],
    )
    return text, markup


@router.callback_query(F.data.startswith(CARD_CB))
async def admin_review_card(callback: CallbackQuery) -> None:
    log_id = int(callback.data.rsplit(":", 1)[-1])
    log = await _log(log_id)
    if log is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    await callback.answer()
    await admin_ui.show(callback, *_card(log))


@router.callback_query(F.data.startswith(PICK_CB))
async def admin_review_pick(callback: CallbackQuery) -> None:
    """Выбор категории вручную — кадр макета 349:381."""
    log_id = int(callback.data.rsplit(":", 1)[-1])
    if await _log(log_id) is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    await callback.answer()
    # `admin_log_keyboard` синхронная и читает кэш как есть: на холодном кэше
    # она показала бы названия из кода вместо тех, что стоят в базе.
    await tariffs.ensure_loaded()
    await admin_ui.show(
        callback,
        T.CATEGORY_PICK.format(number=log_id),
        admin_log_keyboard(log_id, back_cb=f"{CARD_CB}{log_id}"),
    )


def _saved_screen(log_id: int, category: str, ad_text: str) -> tuple[str, InlineKeyboardMarkup]:
    """«Категория сохранена» — кадр макета 349:404.

    Текст объявления на экране повторяется намеренно: администратор проверяет
    подряд, и без него непонятно, какому именно объявлению досталась категория.
    """
    text = T.CATEGORY_SAVED.format(
        number=log_id,
        text=_preview(ad_text, CARD_TEXT_LIMIT),
        category=tariffs.get(category).label,
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [admin_ui.back_button(REVIEW_CB)],
        ],
    )
    return text, markup


async def _save_verdict(log_id: int, category: str, *, verdict: str, source: str) -> bool:
    async with SessionLocal() as session:
        log = await session.scalar(select(ClassificationLog).where(ClassificationLog.id == log_id))
        if log is None:
            return False
        log.admin_verdict = verdict
        log.final_category = category
        session.add(TrainingSample(text=log.text, target_category=category, source=source))
        await session.commit()
    return True


@router.callback_query(F.data.startswith("adm:ok:"))
async def admin_review_confirm(callback: CallbackQuery) -> None:
    """«Подтвердить» — категория модели признана верной."""
    log_id = int(callback.data.rsplit(":", 1)[-1])
    log = await _log(log_id)
    if log is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    category = log.predicted_category
    await _save_verdict(log_id, category, verdict="approved", source="admin_ok")
    await callback.answer("Сохранено")
    await admin_ui.show(callback, *_saved_screen(log_id, category, log.text))


@router.callback_query(F.data.startswith("adm:fix:"))
async def admin_review_fix(callback: CallbackQuery) -> None:
    """«Изменить категорию» — администратор присвоил свою."""
    _, _, raw_id, category = callback.data.split(":", 3)
    log_id = int(raw_id)
    log = await _log(log_id)
    if log is None or not await _save_verdict(log_id, category, verdict="fixed", source="admin_fix"):
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    await callback.answer(tariffs.get(category).label)
    await admin_ui.show(callback, *_saved_screen(log_id, category, log.text))


# ------------------------------------------------------------ обученные примеры


@router.callback_query(F.data == SAMPLES_CB)
@router.callback_query(F.data.startswith(SAMPLES_PAGE_CB))
async def admin_review_samples(callback: CallbackQuery) -> None:
    """Накопленная выборка — из чего модель учится на следующем переобучении."""
    await callback.answer()
    page = int(callback.data.rsplit(":", 1)[-1]) if callback.data.startswith(SAMPLES_PAGE_CB) else 0
    async with SessionLocal() as session:
        rows = list(
            (await session.scalars(select(TrainingSample).order_by(desc(TrainingSample.id)))).all(),
        )

    shown, page, pages = page_slice(rows, page, SAMPLES_PER_PAGE)
    buttons = []
    pager = pager_row(SAMPLES_PAGE_CB, page, pages)
    if pager:
        buttons.append(pager)
    buttons.append([admin_ui.back_button(REVIEW_CB)])

    if not rows:
        await admin_ui.show(
            callback,
            T.CATEGORIES_SAMPLES_EMPTY,
            InlineKeyboardMarkup(inline_keyboard=buttons),
        )
        return

    lines = [T.CATEGORIES_SAMPLES.format(count=len(rows)), ""]
    for sample in shown:
        label = tariffs.get(sample.target_category).label
        lines.append(f"• <b>{label}</b>\n  {_preview(sample.text, SAMPLE_PREVIEW)}")
    await admin_ui.show(
        callback,
        "\n".join(lines),
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ------------------------------------------------------- пополнение выборки


def _retrain_offer() -> InlineKeyboardMarkup:
    """Экран после добавления примеров: сразу переобучить или вернуться."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORIES_RETRAIN_NOW,
                    callback_data=RETRAIN_CB,
                    style=STYLE_MAIN,
                ),
            ],
            [admin_ui.back_button(REVIEW_CB)],
        ],
    )


def _back_to_review() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[admin_ui.back_button(REVIEW_CB)]])


@router.callback_query(F.data == ADD_CB)
async def admin_sample_add(callback: CallbackQuery, state: FSMContext) -> None:
    """«Добавить пример» — текстом, файлом или фото."""
    await state.set_state(AdminFlow.ml_sample_text)
    await callback.answer()
    await admin_ui.prompt(callback, T.SAMPLE_ASK, _back_to_review())


@router.callback_query(F.data == FILE_CB)
async def admin_sample_file(callback: CallbackQuery, state: FSMContext) -> None:
    """«Загрузить файл» — пачка примеров одним документом или архивом."""
    await state.set_state(AdminFlow.ml_sample_file)
    await callback.answer()
    await admin_ui.prompt(
        callback,
        T.FILE_ASK.format(formats=supported_formats_hint()),
        _back_to_review(),
    )


@router.message(AdminFlow.ml_sample_text, F.photo)
@router.message(AdminFlow.ml_sample_file, F.photo)
async def admin_sample_photo(message: Message, state: FSMContext) -> None:
    await _import_photo(message, state)


@router.message(AdminFlow.ml_sample_text, F.document)
@router.message(AdminFlow.ml_sample_file, F.document)
async def admin_sample_document(message: Message, state: FSMContext) -> None:
    await _import_file(message, state)


@router.message(AdminFlow.ml_sample_text)
async def admin_sample_text(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if len(text) < MIN_SAMPLE_LEN:
        await admin_ui.show(message, T.SAMPLE_TOO_SHORT, _back_to_review(), edit=False)
        return
    await _ask_category(message, state, text, T.SAMPLE_PICK_CATEGORY)


@router.message(AdminFlow.ml_sample_file)
async def admin_sample_file_wrong(message: Message) -> None:
    await admin_ui.show(
        message,
        T.FILE_WRONG_TYPE.format(formats=supported_formats_hint()),
        _back_to_review(),
        edit=False,
    )


async def _ask_category(message: Message, state: FSMContext, text: str, template: str) -> None:
    """Один пример без разметки — категорию выбирает администратор."""
    await state.update_data(ml_sample_text=text)
    await state.set_state(AdminFlow.ml_sample_text)
    # Клавиатура читает справочник тарифов как есть — на холодном кэше она
    # показала бы названия из кода вместо тех, что стоят в базе.
    await tariffs.ensure_loaded()
    await admin_ui.show(
        message,
        template.format(preview=_preview(text, CARD_TEXT_LIMIT)),
        admin_ml_category_keyboard(REVIEW_CB),
        edit=False,
    )


async def _save_rows(rows: list[tuple[str, str]], source: str) -> tuple[int, int, int]:
    saved = 0
    skipped = 0
    async with SessionLocal() as session:
        for text, category in rows:
            clean = text.strip()
            if len(clean) < MIN_SAMPLE_LEN:
                skipped += 1
                continue
            session.add(TrainingSample(text=clean, target_category=category, source=source))
            saved += 1
        await session.commit()
        total = int(await session.scalar(select(func.count()).select_from(TrainingSample)) or 0)
    return saved, skipped, total


async def _report_import(message: Message, state: FSMContext, rows: list[tuple[str, str]], source: str) -> None:
    saved, skipped, total = await _save_rows(rows, source)
    await state.clear()
    await admin_ui.show(
        message,
        T.SAMPLES_IMPORTED.format(
            saved=saved,
            skipped=T.SAMPLES_IMPORTED_SKIPPED.format(skipped=skipped) if skipped else "",
            total=total,
        ),
        _retrain_offer(),
        edit=False,
    )


async def _import_photo(message: Message, state: FSMContext) -> None:
    """Скрин объявления: категория берётся из подписи, текст — из OCR."""
    from app.ml.image_ocr import ocr_image_bytes

    category_hint, text_override, caption_err = parse_photo_caption(message.caption or "")
    try:
        tg_file = await message.bot.get_file(message.photo[-1].file_id)
        buffer = await message.bot.download_file(tg_file.file_path)
        ocr_text = (text_override or ocr_image_bytes(buffer.read())).strip()
    except Exception:
        logger.exception("ML photo OCR failed")
        await admin_ui.show(message, T.PHOTO_FAILED, _back_to_review(), edit=False)
        return

    if caption_err and not text_override:
        await admin_ui.show(
            message,
            T.PHOTO_CAPTION_BAD.format(error=caption_err),
            _back_to_review(),
            edit=False,
        )
        return
    if len(ocr_text) < MIN_SAMPLE_LEN:
        await admin_ui.show(message, T.PHOTO_TOO_SHORT, _back_to_review(), edit=False)
        return
    if category_hint:
        await _report_import(message, state, [(ocr_text, category_hint)], source="admin_photo")
        return
    await _ask_category(message, state, ocr_text, T.SAMPLE_PICK_CATEGORY_OCR)


async def _import_file(message: Message, state: FSMContext) -> None:
    """Пачка примеров: .xlsx / .csv / .json / .zip либо картинка."""
    doc = message.document
    formats = supported_formats_hint()
    limit = MAX_ZIP_SIZE if (doc.file_name or "").lower().endswith(".zip") else MAX_FILE_SIZE
    if doc.file_size and doc.file_size > limit:
        await admin_ui.show(
            message,
            T.FILE_TOO_BIG.format(limit=limit // 1_000_000),
            _back_to_review(),
            edit=False,
        )
        return
    try:
        tg_file = await message.bot.get_file(doc.file_id)
        buffer = await message.bot.download_file(tg_file.file_path)
        rows = parse_training_file(doc.file_name or "upload.xlsx", buffer.read())
    except Exception:
        logger.exception("ML file import failed")
        await admin_ui.show(message, T.FILE_BROKEN.format(formats=formats), _back_to_review(), edit=False)
        return

    if not rows:
        await admin_ui.show(message, T.FILE_EMPTY.format(formats=formats), _back_to_review(), edit=False)
        return
    await _report_import(message, state, rows, source="admin_file")


@router.callback_query(F.data.startswith(CATEGORY_CB))
async def admin_sample_save(callback: CallbackQuery, state: FSMContext) -> None:
    """Категория выбрана — пример уходит в выборку."""
    category = callback.data.rsplit(":", 1)[-1]
    text = (await state.get_data()).get("ml_sample_text")
    if not text:
        await callback.answer("Сначала пришлите текст", show_alert=True)
        return
    _saved, _skipped, total = await _save_rows([(text, category)], source="admin_add")
    await state.clear()
    await callback.answer("Сохранено")
    await admin_ui.show(
        callback,
        T.SAMPLE_SAVED.format(category=tariffs.get(category).label, total=total),
        _retrain_offer(),
    )


# ---------------------------------------------------------------- переобучение


@router.callback_query(F.data == RETRAIN_CB)
async def admin_retrain(callback: CallbackQuery, state: FSMContext) -> None:
    """«Переобучить» — исходная выборка заказчика плюс всё, что накопила панель."""
    await state.clear()
    await callback.answer("Обучение…")
    settings = get_settings()
    try:
        metrics = await train_and_save(
            Path(settings.training_data_path),
            Path(settings.ml_model_path),
        )
        get_classifier().reload()
    except Exception as exc:
        logger.exception("Retrain failed")
        await admin_ui.show(callback, T.RETRAIN_FAILED.format(error=exc), _back_to_review())
        return
    await admin_ui.show(
        callback,
        T.RETRAINED.format(samples=metrics.get("samples", 0), accuracy=metrics.get("accuracy", 0)),
        _back_to_review(),
    )


# ------------------------------------------------------------ логи классификаций


@router.callback_query(F.data == LOGS_CB)
@router.callback_query(F.data.startswith(LOGS_PAGE_CB))
async def admin_logs(callback: CallbackQuery) -> None:
    """Решения модели — и те, что уже проверены, в отличие от списка без вердикта."""
    await callback.answer()
    page = int(callback.data.rsplit(":", 1)[-1]) if callback.data.startswith(LOGS_PAGE_CB) else 0
    async with SessionLocal() as session:
        rows = list(
            (await session.scalars(select(ClassificationLog).order_by(desc(ClassificationLog.id)))).all(),
        )

    shown, page, pages = page_slice(rows, page, LOGS_PER_PAGE)
    buttons = [
        [
            InlineKeyboardButton(
                text=f"#{log.id} · {_preview(log.text, BUTTON_PREVIEW)}",
                callback_data=f"{LOG_CARD_CB}{log.id}",
                style=STYLE_PLAIN,
            ),
        ]
        for log in shown
    ]
    pager = pager_row(LOGS_PAGE_CB, page, pages)
    if pager:
        buttons.append(pager)
    buttons.append([admin_ui.back_button(REVIEW_CB)])

    text = T.LOGS.format(count=len(rows)) if rows else T.LOGS_EMPTY
    await admin_ui.show(callback, text, InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith(LOG_CARD_CB))
async def admin_log_card(callback: CallbackQuery) -> None:
    """Карточка лога: видно уверенность модели и прежний вердикт."""
    log = await _log(int(callback.data.rsplit(":", 1)[-1]))
    if log is None:
        await callback.answer("Объявление не найдено", show_alert=True)
        return
    await callback.answer()
    verdicts = {"approved": T.LOG_VERDICT_APPROVED, "fixed": T.LOG_VERDICT_FIXED}
    text = T.LOG_CARD.format(
        number=log.id,
        text=_preview(log.text, CARD_TEXT_LIMIT),
        category=tariffs.get(log.final_category or log.predicted_category).label,
        confidence=float(log.confidence or 0),
        verdict=verdicts.get(log.admin_verdict or "", ""),
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=T.BTN_CATEGORY_CHANGE,
                    callback_data=f"{PICK_CB}{log.id}",
                    style=STYLE_PLAIN,
                ),
            ],
            [admin_ui.back_button(f"{LOGS_PAGE_CB}0")],
        ],
    )
    await admin_ui.show(callback, text, markup)
