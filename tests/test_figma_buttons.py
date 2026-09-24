"""Сетка кнопок экрана против кадра макета — сколько кнопок в каждом ряду.

Пару к `test_figma_parity.py`, который сверяет только текст. Кнопки не
проверял никто, и 20.09.2026 это дважды вышло боком: заказчик сам нашёл пять
кнопок минут в один ряд вместо 2+2+1, а сверка, написанная следом, нашла ещё
два разошедшихся экрана, о которых никто не знал.

Подписи проверялись и раньше (`test_button_labels`, `test_admin_button_widths`),
но только на ширину — влезает ли текст. Сколько кнопок делят ряд, не смотрела
ни одна проверка.

Статически сетку не снять: клавиатуры собираются внутри обработчиков из
данных. Поэтому экран поднимается по-настоящему — обработчик вызывается с
поддельным Telegram, и разбирается отданная им разметка. Всё это живёт в
`scripts/figma_buttons.py`, тест только прогоняет его и держит список
осознанных отступлений.

Сверяется форма, а не подписи: на экранах списков подписи приходят из базы, а
образцы в макете свои. Ряды-записи и пагинация из сравнения выброшены.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DUMP = ROOT / "docs" / "design" / "figma.json"
sys.path.insert(0, str(ROOT / "scripts"))

pytestmark = pytest.mark.skipif(
    not DUMP.exists(),
    reason="нет docs/design/figma.json — выгрузите дамп макета",
)

# Осознанные отступления: бот и кадр расходятся, и прав бот.
#
# Правило заказчика (task6.md, 17.09.2026): «проверяем экраны на текст, сетку
# инлайн-кнопок — если не умещается, правим макет или заменяем слова». В
# половину ряда влезает 16 знакомест (`test_admin_button_widths.ROW_LIMITS`),
# поэтому пара с длинной подписью в макете — это правка макета, а не бота.
# Все записи ниже заведены в docs/figma_fixes_pending.md.
KNOWN = {
    # Белый список нарисован дважды — `338:453` из меню панели и `349:522` из
    # «Управления». Пару «🔎 Найти пользователя» (21 знакоместо) развели пока
    # только на первом: второй кадр всплыл 20.09.2026, когда бот начал
    # различать эти два пути. Правка заведена в `SPLITS` плагина.
    "admin_manage_wl_menu": (
        "пара «🔎 Найти пользователя» (21) в половину ряда не влезает; "
        "на 338:453 уже разнесено, на 349:522 ждёт прогона плагина"
    ),
}

# Семь записей, стоявших здесь 20.09.2026, сняты в тот же день: правки доехали
# в клон плагином, и сторож ниже про них сказал. Список не для истории —
# история в docs/figma_fixes_pending.md.


async def _rows(world, bot, monkeypatch):
    """Форма клавиатуры каждого экрана: (обработчик, кадр, макет, бот)."""
    from figma_buttons import anchors, bot_rows, design_rows, open_screen, resolve_data
    from figma_read import frames_of

    monkeypatch.setattr("app.services.access.is_admin", lambda user_id: True)
    monkeypatch.setattr("app.services.access.is_staff", lambda user_id: True)
    monkeypatch.setattr("app.handlers.admin.common._is_admin", lambda user_id: True)

    import importlib

    frames = {f["id"]: f for f in frames_of(json.loads(DUMP.read_text()))}
    out = []
    for module_name, func_name, expression, ids in anchors():
        module = importlib.import_module(f"app.handlers.admin.{module_name}")
        monkeypatch.setattr(module, "_is_admin", lambda user_id: True, raising=False)
        handler = getattr(module, func_name, None)
        data = resolve_data(module, expression)
        if handler is None or data is None:
            continue
        try:
            markup = await open_screen(data, handler)
        except Exception:                             # noqa: BLE001
            continue                                  # экран требует данных — не наш случай
        if markup is None:
            continue
        for frame_id in ids:
            frame = frames.get(frame_id)
            if frame is not None:
                out.append((func_name, frame_id, design_rows(frame), bot_rows(markup)))
    return out


async def test_the_check_actually_covers_screens(world, bot, monkeypatch):
    """Страховка: сломается подъём экранов — сверка молча станет пустой."""
    rows = await _rows(world, bot, monkeypatch)
    assert len(rows) >= 20, f"поднялось всего {len(rows)} экранов"


async def test_button_grid_matches_the_frame(world, bot, monkeypatch):
    """Ряды кнопок совпадают с кадром — либо расхождение описано в KNOWN."""
    bad = []
    for func_name, frame_id, design, got in await _rows(world, bot, monkeypatch):
        if design == got or func_name in KNOWN:
            continue
        bad.append(f"{func_name} / {frame_id}: в макете {design}, в боте {got}")
    assert not bad, "сетка кнопок разошлась с макетом:\n  " + "\n  ".join(bad)


async def test_known_exceptions_are_still_needed(world, bot, monkeypatch):
    """Расхождение исчезло — уберите запись из KNOWN, иначе список зарастёт."""
    stale = [
        func_name
        for func_name, _frame, design, got in await _rows(world, bot, monkeypatch)
        if func_name in KNOWN and design == got
    ]
    assert not stale, f"эти расхождения исчезли, уберите их из KNOWN: {sorted(set(stale))}"
