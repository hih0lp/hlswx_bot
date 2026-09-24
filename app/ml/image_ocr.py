from __future__ import annotations

import logging
import re
from io import BytesIO

logger = logging.getLogger("ml.image_ocr")

_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def is_image_filename(filename: str) -> bool:
    return (filename or "").lower().endswith(_IMAGE_EXTENSIONS)


def category_from_image_stem(stem: str) -> str | None:
    """VACANCY_ad_01 → VACANCY, 1200_sample → price category."""
    from app.ml.import_samples import resolve_category_token

    stem = (stem or "").strip()
    if not stem:
        return None
    head = re.split(r"[\s\-–—_]+", stem, maxsplit=1)[0]
    return resolve_category_token(head)


def ocr_image_bytes(data: bytes) -> str:
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("OCR недоступен: не установлены Pillow/pytesseract") from exc

    try:
        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        text = pytesseract.image_to_string(image, lang="rus+eng")
    except pytesseract.TesseractNotFoundError as exc:
        raise RuntimeError("OCR недоступен: tesseract не установлен на сервере") from exc
    except Exception as exc:
        logger.exception("OCR failed")
        raise RuntimeError(f"OCR ошибка: {exc}") from exc

    cleaned = re.sub(r"[ \t]+\n", "\n", text or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned
