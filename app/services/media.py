"""Объявление, присланное сообщением: текст, подпись, фото.

Пользователь присылает объявление тремя способами — текстом, фото с подписью
или одним фото (правка заказчика от 17.09.2026). До неё хендлеры читали только
`message.text`, а у сообщения с картинкой он пуст: подпись лежит в
`message.caption`. Из-за этого пересланное объявление с фото упиралось в экран
«Не удалось определить категорию», хотя текст в сообщении был.

Здесь эти три случая сведены к одному результату: текст для классификатора и
`file_id` картинки для публикации. Логика вынесена из панели (раздел «Обучение
модели» уже умел распознавать скрины), чтобы не держать две копии.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("media")


@dataclass(frozen=True)
class AdContent:
    """Разобранное сообщение с объявлением."""

    text: str
    photo_id: str | None = None
    #: Текст получен распознаванием картинки, а не написан руками.
    from_ocr: bool = False

    @property
    def empty(self) -> bool:
        return not self.text


def photo_file_id(message) -> str | None:
    """`file_id` самого крупного варианта картинки.

    Медиагруппу (несколько фото одним сообщением) Telegram присылает разными
    апдейтами — берём то фото, к которому привязана подпись.
    """
    photos = getattr(message, "photo", None)
    return photos[-1].file_id if photos else None


async def read_ad_content(message) -> AdContent:
    """Собрать текст и картинку из сообщения с объявлением.

    Фото без подписи прогоняется через OCR: заказчик просил принимать и такие
    объявления, а тариф всё равно считается по тексту. Если OCR недоступен или
    ничего не разобрал, возвращается пустой текст — вызывающий покажет свой
    экран с просьбой добавить описание.
    """
    photo_id = photo_file_id(message)
    text = (message.text or message.caption or "").strip()
    if text or not photo_id:
        return AdContent(text=text, photo_id=photo_id)

    recognized = await ocr_photo(message)
    return AdContent(text=recognized, photo_id=photo_id, from_ocr=bool(recognized))


async def ocr_photo(message) -> str:
    """Распознать текст на присланной картинке. Пустая строка — не получилось."""
    from app.ml.image_ocr import ocr_image_bytes

    try:
        tg_file = await message.bot.get_file(photo_file_id(message))
        buffer = await message.bot.download_file(tg_file.file_path)
        return ocr_image_bytes(buffer.read()).strip()
    except Exception:
        logger.exception("Не удалось распознать объявление на картинке")
        return ""
