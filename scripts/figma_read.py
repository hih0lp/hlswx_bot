"""Чтение экрана макета из дампа docs/design/figma.json.

Общий модуль для `figma_export_text.py`, `figma_diff.py` и теста паритета:
превращает кадр Figma в то, что увидит пользователь в Telegram — строки
сообщения и ряды кнопок.

Три вещи, на которых легко ошибиться, и как они решены здесь.

**Горизонтальные ряды.** Строка вида «📍 Москва · 💬 2 чата · 📊 До 3 публикаций
в день» нарисована фреймом `layoutMode=HORIZONTAL`, где между текстовыми нодами
стоят `ELLIPSE` — точки-разделители. Если читать ноды подряд, одна строка
распадается на три, и бот начинает «не совпадать» там, где всё верно.

**Кнопка — это скругление.** У кнопки `cornerRadius = 40` и сплошная заливка.
Пузырь сообщения выглядит так же, поэтому первый скруглённый фрейм кадра
считаем сообщением, остальные — кнопками. Кнопка бывает многострочной: в
списках внутри неё лежат два-три горизонтальных ряда.

**Отступы.** В Telegram вертикальный интервал бывает только кратен строке,
в макете он произвольный. Макет нарисован по сетке 8, и зазоров на всей
странице ровно три: 8 и 16 стоят внутри блока, 24 — на его границе (после
заголовка, между сводкой и статусом). Значит 8 и 16 — обычный перенос, 24 и
больше — пустая строка. Считаем по absoluteBoundingBox, а не по itemSpacing:
тот меряется от края контейнера и на вложенных рядах врёт.

Отношение «зазор ÷ высота строки» для этого не годится, хотя раньше стояло
именно оно: у заголовка кегль крупнее, и его 24px дают 0.80 против 0.71 у
16px внутри сводки — границы блоков и строки внутри них перемешиваются.
Пиксели разделяют их начисто.
"""

from __future__ import annotations

# Скругление кнопки и пузыря сообщения.
ROUND = 20

# Имена фреймов-сообщений: у некоторых кадров их два.
MESSAGE_NAMES = {"Первое сообщение", "Сообщение", "Второе сообщение"}

# Зазор в пикселях, начиная с которого читается пустая строка. Между 16 и 24
# — соседние ступени сетки макета, поэтому порог стоит ровно посередине.
BLANK_GAP = 20

# Разделитель между колонками горизонтального ряда.
DOT = " · "

LINE_BREAKS = ("\n", "\r", " ", " ")


def is_round(node) -> bool:
    return (node.get("cornerRadius") or 0) >= ROUND


def _box(node):
    return node.get("absoluteBoundingBox") or {}


def _split(text: str) -> list[str]:
    """Переносы внутри текстовой ноды — это отдельные строки."""
    for mark in LINE_BREAKS[1:]:
        text = text.replace(mark, "\n")
    return [part.strip() for part in text.split("\n") if part.strip()]


def _units(node, out):
    """Единицы строки в порядке чтения: текстовая нода или горизонтальный ряд."""
    if node.get("type") == "TEXT":
        out.append(node)
        return out
    if node.get("layoutMode") == "HORIZONTAL":
        texts = [c for c in (node.get("children") or []) if c.get("type") == "TEXT"]
        # ряд из нескольких колонок — одна строка; ряд с одним текстом
        # разбирать как ряд незачем, но и вреда нет
        if texts:
            out.append(node)
            return out
    for child in node.get("children") or []:
        _units(child, out)
    return out


def _unit_text(unit) -> list[str]:
    if unit.get("type") == "TEXT":
        return _split(unit.get("characters") or "")
    parts = []
    for child in unit.get("children") or []:
        if child.get("type") == "TEXT":
            parts.extend(_split(child.get("characters") or ""))
    return [DOT.join(parts)] if parts else []


def _sorted_units(node) -> list:
    units = _units(node, [])
    units.sort(key=lambda u: (round(_box(u).get("y", 0)), _box(u).get("x", 0)))
    return units


def _join(units, *, blanks: bool) -> list[str]:
    """Единицы -> строки, с пустыми строками по зазорам между ними."""
    lines: list[str] = []
    prev = None
    for unit in units:
        text = _unit_text(unit)
        if not text:
            continue
        if lines and blanks and prev is not None:
            box, before = _box(unit), _box(prev)
            gap = box.get("y", 0) - (before.get("y", 0) + before.get("height", 0))
            if gap >= BLANK_GAP:
                lines.append("")
        lines.extend(text)
        prev = unit
    return lines


def lines_of(node, *, blanks: bool = True) -> list[str]:
    """Строки узла. blanks=False — для подписи кнопки: там пустых строк нет."""
    return _join(_sorted_units(node), blanks=blanks)


def button_label(node) -> str:
    """Подпись кнопки. В списках она многострочная — как нарисовано."""
    return "\n".join(lines_of(node, blanks=False))


def render(frame) -> list[tuple[str, object]]:
    """Кадр -> блоки ('text', [строки]) и ('row', [подписи кнопок]).

    Порядок блоков — как на экране: сообщение, затем ряды клавиатуры.
    """
    # Сначала собираем плоский список того, что на экране, — текстовые
    # единицы и ряды кнопок, — и только потом склеиваем. Иначе зазор между
    # соседними блоками текста теряется, и пустая строка между ними пропадает.
    found: list[tuple[str, object, float, float]] = []
    seen_message = False

    def push(kind, payload, node):
        box = _box(node)
        found.append((kind, payload, box.get("y", 0), box.get("x", 0)))

    def visit(node):
        nonlocal seen_message
        for child in node.get("children") or []:
            kind = child.get("type")
            if kind == "TEXT":
                push("unit", child, child)
                continue
            if kind != "FRAME":
                continue
            if is_round(child) and (
                child.get("name") in MESSAGE_NAMES or not seen_message
            ):
                seen_message = True
                for unit in _sorted_units(child):
                    push("unit", unit, unit)
                continue
            if is_round(child):
                push("row", [button_label(child)], child)
                continue
            kids = [c for c in (child.get("children") or []) if c.get("type") == "FRAME"]
            if (child.get("layoutMode") == "HORIZONTAL" and kids
                    and all(is_round(k) for k in kids)):
                push("row", [button_label(k) for k in kids], child)
                continue
            if child.get("layoutMode") == "HORIZONTAL" and any(
                c.get("type") == "TEXT" for c in child.get("children") or []
            ):
                push("unit", child, child)
                continue
            visit(child)

    visit(frame)
    found.sort(key=lambda item: (round(item[2]), item[3]))

    blocks: list[tuple[str, object]] = []
    batch: list = []

    def flush():
        if batch:
            blocks.append(("text", _join(batch, blanks=True)))
            batch.clear()

    for kind, payload, _y, _x in found:
        if kind == "unit":
            batch.append(payload)
            continue
        flush()
        blocks.append(("row", payload))
    flush()
    return blocks


def buttons_of(frame) -> list[str]:
    return [label for kind, payload in render(frame) if kind == "row"
            for label in payload]


def frames_of(dump, page_name="Flows"):
    """Кадры страницы в порядке чтения по холсту: сверху вниз, слева направо."""
    page = next(p for p in dump["document"]["children"] if page_name in p["name"])
    frames = [c for c in page["children"] if c["type"] == "FRAME"]
    frames.sort(key=lambda f: (round(_box(f).get("y", 0) / 100), _box(f).get("x", 0)))
    return frames
