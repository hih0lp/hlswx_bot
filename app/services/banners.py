"""Баннеры разделов из макета Figma (ТЗ 6.9).

Каждому экрану макета соответствует свой баннер; файлы лежат в assets/banners,
имена совпадают с заголовком на картинке. Telegram отдаёт file_id после первой
загрузки — кэшируем его в assets/banner_ids.json, иначе каждое открытие раздела
заново заливало бы ~800 КБ.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import (
    LinkPreviewOptions,
    FSInputFile,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    ReplyKeyboardMarkup,
)

logger = logging.getLogger("banners")

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
BANNERS_DIR = ASSETS_DIR / "banners"
CACHE_PATH = ASSETS_DIR / "banner_ids.json"

# Подпись к фото в Telegram ограничена 1024 символами. Более длинный экран
# («Правила») уходит отдельным сообщением следом за баннером.
CAPTION_LIMIT = 1024

MAIN_MENU = "main_menu"
PROFILE = "profile"
RULES = "rules"
AD = "ad"
PUBLICATION = "publication"
SUBSCRIPTION = "subscription"
TOPUP = "topup"
INTEGRATION = "integration"
FRANCHISE = "franchise"
ATTENTION = "attention"
ADMIN = "admin"

_FILES = {
    MAIN_MENU: "main_menu.jpg",        # ГЛАВНОЕ МЕНЮ
    PROFILE: "profile.jpg",            # ПРОФИЛЬ
    RULES: "rules.jpg",                # ПРАВИЛА
    AD: "ad.jpg",                      # ОБЪЯВЛЕНИЕ — шаги оформления подписки
    PUBLICATION: "publication.jpg",    # ПУБЛИКАЦИЯ — шаги публикации по подписке
    SUBSCRIPTION: "subscription.jpg",  # ПОДПИСКА — мои подписки
    TOPUP: "topup.jpg",                # ПОПОЛНЕНИЕ
    INTEGRATION: "integration.jpg",    # ИНТЕГРАЦИЯ
    FRANCHISE: "franchise.jpg",        # ФРАНШИЗА
    ATTENTION: "attention.jpg",        # ВНИМАНИЕ — подписка закончилась
    ADMIN: "admin.jpg",                # АДМИНКА
}


def _load_cache() -> dict[str, str]:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        logger.debug("Не удалось сохранить кэш file_id баннеров", exc_info=True)


# Экран без баннера. В макете картинка стоит не на каждом кадре, а только в
# начале раздела — остальные экраны это обычные сообщения. Передать сюда `None`
# и значит «экран без картинки»: `banner_path` вернёт None, и весь тракт ниже
# (отправка, перерисовка) уже умеет работать с текстовым сообщением.
PLAIN = None


def banner_path(key: str | None) -> Path | None:
    name = _FILES.get(key) if key else None
    if not name:
        return None
    path = BANNERS_DIR / name
    return path if path.exists() else None


async def _custom_banner_file_id(key: str | None) -> str | None:
    """Return this premium tenant's Telegram file_id, if one was uploaded."""
    if not key:
        return None
    from app.services.tenant import current_partner_id, current_partner_tier
    partner_id = current_partner_id()
    if not partner_id or current_partner_tier() != "premium":
        return None
    from sqlalchemy import select
    from app.db.session import SessionLocal
    from app.models.entities import AppSetting
    async with SessionLocal() as session:
        setting = await session.scalar(select(AppSetting).where(
            AppSetting.partner_id == partner_id,
            AppSetting.key == f"premium_banner:{key}",
        ))
        return setting.value if setting and setting.value else None


# Последний показанный экран в каждом чате. Нужен, потому что главное меню —
# reply-клавиатура: её нажатие приходит обычным сообщением пользователя, и
# сообщения с экраном, который надо обновить, в обработчике нет. Держим в
# памяти процесса: после перезапуска бот просто отправит экран заново.
_LAST_SCREEN: dict[int, int] = {}
_LAST_SCREEN_LIMIT = 20_000


def remember_screen(chat_id: int, message_id: int) -> None:
    if len(_LAST_SCREEN) > _LAST_SCREEN_LIMIT:
        _LAST_SCREEN.clear()
    _LAST_SCREEN[chat_id] = message_id


def forget_screen(chat_id: int) -> None:
    """Экран перестал быть последним сообщением — обновлять его больше нельзя."""
    _LAST_SCREEN.pop(chat_id, None)


# Экран со ссылками на группы («Города и чаты») не должен обрастать карточкой
# предпросмотра первой ссылки: `admin_ui.show(..., preview=False)` ставит флаг.
NO_PREVIEW: ContextVar[bool] = ContextVar("banners_no_preview", default=False)


def _preview() -> LinkPreviewOptions | None:
    return LinkPreviewOptions(is_disabled=True) if NO_PREVIEW.get() else None


async def send_banner(
    message: Message,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    """Показать экран макета новым сообщением: баннер + текст + кнопки."""
    await send_banner_to(message.bot, message.chat.id, key, text, reply_markup)


async def send_banner_to(
    bot,
    chat_id: int,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
) -> None:
    """Send the tenant's premium banner, falling back to the platform asset."""
    path = banner_path(key)
    custom_file_id = await _custom_banner_file_id(key)
    if path is None and not custom_file_id:
        sent = await bot.send_message(
            chat_id, text, reply_markup=reply_markup, link_preview_options=_preview(),
        )
        remember_screen(chat_id, sent.message_id)
        return

    long_text = len(text) > CAPTION_LIMIT
    caption = None if long_text else text
    markup = None if long_text else reply_markup
    cache = _load_cache()
    candidates = []
    if custom_file_id:
        candidates.append(custom_file_id)
    if key and cache.get(key) and cache[key] not in candidates:
        candidates.append(cache[key])
    sent = None
    for file_id in candidates:
        try:
            sent = await bot.send_photo(
                chat_id, photo=file_id, caption=caption, reply_markup=markup, request_timeout=60,
            )
            break
        except Exception:
            logger.warning("Не удалось отправить file_id баннера %s", key)

    if sent is None and path is not None:
        try:
            sent = await bot.send_photo(
                chat_id, photo=FSInputFile(path), caption=caption, reply_markup=markup, request_timeout=120,
            )
            photo = getattr(sent, "photo", None)
            if photo and key:
                cache[key] = photo[-1].file_id
                _save_cache(cache)
        except TelegramForbiddenError:
            raise
        except Exception:
            logger.exception("Не удалось отправить баннер %s", key)

    if sent is None:
        fallback = await bot.send_message(
            chat_id, text, reply_markup=reply_markup, link_preview_options=_preview(),
        )
        remember_screen(chat_id, fallback.message_id)
        return

    if long_text:
        await bot.send_message(
            chat_id, text, reply_markup=reply_markup, link_preview_options=_preview(),
        )
        forget_screen(chat_id)
        return
    remember_screen(chat_id, sent.message_id)


async def _drop_markup(message: Message) -> None:
    """Снять кнопки с покинутого экрана, чтобы по ним нельзя было кликнуть снова."""
    try:
        await message.edit_reply_markup(reply_markup=None)
    except Exception:
        logger.debug("Не удалось снять кнопки с сообщения", exc_info=True)


async def drop_screen_markup(bot, chat_id: int) -> None:
    """Снять кнопки с последнего показанного экрана в чате.

    Вызывается перед отправкой нового экрана: покинутый остаётся в переписке
    как текст, но кликнуть по его кнопкам уже нельзя.
    """
    message_id = _LAST_SCREEN.get(chat_id)
    if message_id is None:
        return
    forget_screen(chat_id)
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None,
        )
    except TelegramBadRequest as exc:
        if not _not_modified(exc):
            logger.debug("Кнопки с прошлого экрана не сняты: %s", exc)
    except Exception:
        logger.debug("Кнопки с прошлого экрана не сняты", exc_info=True)


def last_screen_id(chat_id: int) -> int | None:
    """id последнего показанного экрана в чате — например, чтобы потом снять с него кнопки."""
    return _LAST_SCREEN.get(chat_id)


async def _edit_screen(
    bot,
    chat_id: int,
    message_id: int,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None,
) -> bool:
    """Перерисовать экран в конкретном сообщении.

    Экраны — фото с подписью, поэтому текст меняется через editMessageMedia:
    editMessageText на фото не работает. Возвращает False, если так обновить
    нельзя (подпись длиннее лимита, сообщение не фото, Telegram отказал) —
    тогда вызывающий отправляет новое сообщение.
    """
    # Подпись к фото ограничена 1024 символами: длинный экран («Правила»)
    # существует только как фото + отдельное сообщение, редактировать нечего.
    if len(text) > CAPTION_LIMIT:
        return False

    path = banner_path(key)
    custom_file_id = await _custom_banner_file_id(key)
    if path is None and not custom_file_id:
        try:
            await bot.edit_message_text(
                text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup,
                link_preview_options=_preview(),
            )
            return True
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                await _sync_markup(bot, chat_id, message_id, reply_markup)
                return True
            logger.warning("DIAG edit_message_text failed: %s", exc)
            return False
        except Exception:
            logger.exception("Не удалось обновить экран без баннера")
            return False

    cache = _load_cache()
    media_sources = [custom_file_id, cache.get(key)]
    if path is not None:
        media_sources.append(FSInputFile(path))
    seen = set()
    for media in media_sources:
        if media is None or (isinstance(media, str) and media in seen):
            continue
        if isinstance(media, str):
            seen.add(media)
        try:
            sent = await bot.edit_message_media(
                media=InputMediaPhoto(media=media, caption=text, parse_mode="HTML"),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
            )
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                await _sync_markup(bot, chat_id, message_id, reply_markup)
                return True
            # Кэшированный file_id мог устареть — вторым заходом шлём файл.
            logger.debug("Баннер %s не обновился: %s", key, exc)
            continue
        except Exception:
            logger.exception("Не удалось обновить баннер %s", key)
            return False

        if isinstance(sent, Message) and sent.photo and media != custom_file_id and key:
            cache[key] = sent.photo[-1].file_id
            _save_cache(cache)
        return True

    return False


def _not_modified(exc: TelegramBadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


async def _sync_markup(bot, chat_id: int, message_id: int, reply_markup: InlineKeyboardMarkup | None) -> None:
    """Содержимое совпало — но кнопки могли смениться (например, отметки чатов)."""
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=reply_markup,
        )
    except TelegramBadRequest:
        pass
    except Exception:
        logger.debug("Не удалось обновить кнопки", exc_info=True)


async def redraw(
    message: Message,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Перерисовать экран, на кнопку которого нажали.

    Клик по inline-кнопке даёт сообщение самого бота — его и обновляем. Всё
    остальное (команда, кнопка меню, ввод текста) приходит сообщением
    пользователя: там экран уходит новым, перерисовывать нечего.
    """
    if not isinstance(message, Message):
        logger.warning("DIAG redraw: not Message: %s", type(message).__name__)
        return False
    if not (message.from_user and message.from_user.is_bot):
        logger.warning("DIAG redraw: from_user=%r", message.from_user)
        return False

    # Фото в текст и обратно Telegram не превращает: экран с баннером и экран
    # без него — сообщения разного рода. Отсекаем здесь, а не ошибкой от
    # Telegram: иначе попытка перерисовать текстовый экран баннером успевала
    # залить файл на ~800 КБ, прежде чем получить отказ.
    if bool(message.photo) != (banner_path(key) is not None):
        logger.warning("DIAG redraw: photo=%s key=%r", bool(message.photo), key)
        return False

    chat_id = message.chat.id
    target_id = message.message_id
    if not await _edit_screen(message.bot, chat_id, target_id, key, text, reply_markup):
        return False

    remember_screen(chat_id, target_id)
    return True


async def show_screen(
    message: Message,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    *,
    edit: bool = False,
    keep_markup: bool = False,
) -> None:
    """Экран макета: обновить показанный экран или отправить новый.

    `keep_markup=True` — на покинутом экране кнопки остаются (как в админке
    Hammer): он не превращается в «мёртвую» копию, по нему можно нажимать снова.

    Правило заказчика, как в @HammerPaySystemBot: нажатие кнопки внутри
    раздела перерисовывает экран на месте, а новый раздел (команда или кнопка
    меню) уходит новым сообщением — у покинутого экрана просто пропадают
    кнопки. Поэтому edit=True работает только для кликов по inline-кнопкам:
    их обработчик передаёт сюда сообщение бота.

    Бот ничего не удаляет (правка заказчика от 02.10.2026). Если клик пришёл, а
    перерисовать на месте нельзя — экран сменил род (фото ↔ текст: Telegram
    превращать одно в другое не умеет) или подпись длиннее лимита, — у
    покинутого сообщения снимаются inline-кнопки, а экран уходит новым.
    """
    if edit and isinstance(reply_markup, (InlineKeyboardMarkup, type(None))):
        if await redraw(message, key, text, reply_markup):
            return
        if message.from_user and message.from_user.is_bot:
            # Перерисовать не вышло: либо экран сменил род (фото ↔ текст),
            # либо подпись длиннее лимита. Покинутый остаётся без кнопок.
            if not keep_markup:
                await _drop_markup(message)
            await send_banner(message, key, text, reply_markup)
            return

    await show_new_screen(message, key, text, reply_markup, keep_markup=keep_markup)


async def show_new_screen(
    message: Message,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    *,
    keep_markup: bool = False,
) -> None:
    """Экран новым сообщением: у прошлого остаётся текст, но не кнопки.

    С `keep_markup=True` кнопки прошлого экрана тоже остаются.
    """
    if not keep_markup:
        await drop_screen_markup(message.bot, message.chat.id)
    await send_banner(message, key, text, reply_markup)


async def show_prompt(
    message: Message,
    key: str | None,
    text: str,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    *,
    edit: bool = False,
) -> None:
    """Шаг, на котором бот ждёт ввод: текст объявления, пример, контакт, сумму.

    Правило то же, что у обычных экранов. Если на шаг пришли кликом по
    inline-кнопке (`edit=True` и сообщение бота), он перерисовывается на месте:
    между экранами ничего не появилось, и новое сообщение выглядело бы как
    дубль. А если перед шагом пользователь что-то написал (ввёл текст
    объявления, нажал кнопку меню), экран уходит новым сообщением — просьба
    должна остаться в переписке рядом с ответом, а не затираться.
    """
    await show_screen(message, key, text, reply_markup, edit=edit)
