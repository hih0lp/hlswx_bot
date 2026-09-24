"""Подписи кнопок админ-панели помещаются в кнопку (ТЗ этапа 2, 6.8 и 6.13).

Заказчик просит: «проверяем экраны на текст, сетку Инлайн кнопок — если не
умещается, правим макет или заменяем слова» (task6.md, 17.09.2026). Хвост,
который не влез, Telegram обрезает молча, и заметно это только на телефоне:
так пропали «Пополнить баланс», «Города и группы» и «Добавить город».

Проверка идёт по исходникам разделов: клавиатуры собираются прямо в
обработчиках, вызвать их без живого Telegram нельзя, а ряды в коде записаны
списками — по ним и видно, сколько кнопок делят ширину.

Ряды, собранные циклом или из данных (города, чаты, записи белого списка),
сюда не попадают: их подписи приходят из базы, а не из `admin_texts`.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core import admin_texts as T
from app.core.texts import BTN_BACK
from app.services.admin_ui import BTN_TO_PANEL

ROOT = Path(__file__).resolve().parents[1]
ADMIN_DIR = ROOT / "app" / "handlers" / "admin"
BROADCAST = ROOT / "app" / "handlers" / "admin_broadcast.py"

# Мерим не длину строки, а ширину на экране: значок занимает две позиции, и
# разница решающая — «⭐ Белый список» (15) помещается, а «➕ Добавить город»
# (17) обрезается, хотя в символах они почти равны.
#
# Пределы сняты с живых экранов: полный ряд держит 28 (tests/test_button_labels.py),
# ряд из двух кнопок — 16 (по скриншотам заказчика от 17.09.2026: «Города и
# чаты» и «Белый список» видны целиком, «Добавить город» обрезано).
ROW_LIMITS = {1: 28, 2: 16, 3: 10}

# Считаем ширину той же функцией, что и бот, когда обрезает подписи
# (`textfmt.fit_label`): две копии правила разъехались бы, и тест перестал бы
# проверять то, что происходит на самом деле.
from app.services.textfmt import label_width as _line_width  # noqa: E402


def _width(label: str) -> int:
    """Ширина подписи в знакоместах: значок — два, стрелка и буква — одно.

    Подпись бывает многострочной (записи списков — кадры 341:1112, 349:580,
    349:1025), и ограничивает её самая длинная строка, а не сумма: переносы
    расставлены нами, а не клиентом.
    """
    return max((_line_width(line) for line in label.split("\n")), default=0)


def _row_limit(buttons_in_row: int) -> int:
    return ROW_LIMITS.get(buttons_in_row, 8)


def _sources() -> list[Path]:
    return sorted(ADMIN_DIR.glob("*.py")) + [BROADCAST]


def _label(node: ast.AST) -> str | None:
    """Подпись кнопки, если её видно прямо в коде.

    Понимает три записи: строковый литерал, константу из `admin_texts`
    (`T.BTN_...`) и кнопки возврата из `admin_ui`, у которых подпись либо
    своя, либо по умолчанию.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func

    if isinstance(func, ast.Name) and func.id == "InlineKeyboardButton":
        for keyword in node.keywords:
            if keyword.arg != "text":
                continue
            value = keyword.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "T":
                return getattr(T, value.attr, None)
        return None

    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "panel_button":
        return BTN_TO_PANEL
    if name == "back_button":
        if len(node.args) >= 2:
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name) and arg.value.id == "T":
                return getattr(T, arg.attr, None)
            return None
        return BTN_BACK
    return None


def _rows(path: Path) -> list[tuple[int, list[str]]]:
    """Ряды кнопок из файла: (номер строки, подписи)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List) or not node.elts:
            continue
        labels = [_label(item) for item in node.elts]
        # Ряд считаем разобранным, только если поняли каждую его кнопку:
        # иначе ширину делить не на что.
        if all(isinstance(label, str) for label in labels):
            found.append((node.lineno, labels))
    return found


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_labels_fit_their_row(path: Path):
    too_long = []
    for lineno, labels in _rows(path):
        limit = _row_limit(len(labels))
        too_long.extend(
            f"{path.name}:{lineno} {label!r} — ширина {_width(label)}, влезает {limit}"
            for label in labels
            if _width(label) > limit
        )
    assert not too_long, "подписи обрежутся на телефоне:\n" + "\n".join(too_long)


def test_the_check_actually_sees_rows():
    """Страховка от «тест зелёный, потому что ничего не нашёл»."""
    total = sum(len(_rows(path)) for path in _sources())
    assert total > 40, f"разобрано всего {total} рядов — разбор сломался"
