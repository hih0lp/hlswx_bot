"""Уведомление «Требуется ручная правка» убрано (решение заказчика от 14.09.2026).

Правка и закрытие постов, вышедших через ботов-партнёров Hammer и W, не
проходят: на их стороне такой возможности нет. Раньше по каждому такому отказу
администратору уходил отдельный пуш с просьбой поправить сообщение руками —
заказчик попросил этот сценарий убрать.

Счётчик отказов при этом остаётся на экранах результата: скрыть его значило бы
показывать правку успешной там, где часть сообщений не изменилась.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core import admin_texts as T

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / "app" / "handlers" / "publication_actions.py"


def test_manual_edit_push_is_gone():
    source = ACTIONS.read_text(encoding="utf-8")
    assert "_notify_admins_manual_edit" not in source
    assert "Требуется ручная правка" not in source


def test_nobody_promises_a_manual_fix():
    """Обещать ручную правку больше нельзя — её никто не делает."""
    texts = [
        ACTIONS.read_text(encoding="utf-8"),
        T.PUB_EDITED_FAILED,
    ]
    pattern = re.compile(r"вручную|поправит рук|руками")
    for text in texts:
        assert not pattern.search(text), pattern.search(text).group(0)


def test_failure_count_still_surfaces():
    """Отказы по-прежнему видно — и с объяснением причины."""
    line = T.PUB_EDITED_FAILED.format(count=2)
    assert "2" in line
    assert "ботом-партнёром" in line


def test_close_result_names_the_reason_without_promising_a_fix():
    source = ACTIONS.read_text(encoding="utf-8")
    assert "не удалось закрыть: {failed} — опубликованы ботом-партнёром" in source
