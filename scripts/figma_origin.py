#!/usr/bin/env python3
"""Чем наша копия макета отличается от оригинала заказчика.

`figma_screens.py` сверяет бота с макетом. Этот скрипт сверяет **макет с
макетом**: что мы сами в копии поменяли и не разошлось ли это с тем, что
нарисовано у заказчика. Повод завести его был 20.09.2026 — заказчик заметил,
что кнопки минут в копии переложены нами, а в его оригинале стоят иначе, и
выяснять это пришлось вручную.

Читаются оба дампа одним и тем же `figma_read.render`, поэтому сравниваются не
ноды, а то, что увидит пользователь: строки сообщения и **ряды кнопок**.
Последнее здесь главное — расходится обычно именно сетка, а текстовые сверки
её не видят вовсе.

    ./scripts/figma_origin.py                                   # оригинал ↔ копия
    ./scripts/figma_origin.py --only 341:1248 344:1284          # только эти кадры
    ./scripts/figma_origin.py --before docs/design/figma.json.before-plugin.bak

Последняя форма отвечает на другой вопрос — «что изменил последний прогон
плагина»: слева ставится копия до прогона, справа она же после.

Оригинал: «HLSWX Bot», `wJbvRHNGmCH6Owl4OjJ7Ud`. Живьём он не читается — у
рабочего аккаунта нет edit-доступа, и Figma MCP отказывает. Снимок
`figma.json.0907.bak` от 06.09.2026 — единственный, что у нас есть, и он
старше клона: кадров «Города и чаты» и «Тарифы» в нём ещё нет. Такие кадры
скрипт перечисляет отдельно, а не выдаёт за расхождение.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figma_read import frames_of, render  # noqa: E402

ORIGIN = "docs/design/figma.json.0907.bak"
COPY = "docs/design/figma.json"


def _rows(frame) -> list[str]:
    """Кадр — плоским списком строк: «T|» текст, «B|» ряд кнопок."""
    out: list[str] = []
    for kind, payload in render(frame):
        if kind == "text":
            out.extend(f"T| {line}" for line in payload)
        else:
            # Подпись многострочной кнопки склеиваем в строку: перенос внутри
            # ячейки сбил бы выравнивание дифа.
            out.append("B| " + " || ".join(x.replace("\n", " ⏎ ") for x in payload))
    return out


def _snapshot(path: str) -> tuple[dict, dict[str, tuple[str, list[str]]]]:
    if not os.path.exists(path):
        sys.exit(f"нет {path} — сначала выгрузите дамп")
    with open(path) as handle:
        dump = json.load(handle)
    return dump, {f["id"]: (f["name"], _rows(f)) for f in frames_of(dump)}


def _order(frame_id: str) -> tuple[int, int]:
    left, _, right = frame_id.partition(":")
    return int(left), int(right)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--before", default=ORIGIN, help=f"слева (по умолчанию {ORIGIN})")
    p.add_argument("--after", default=COPY, help=f"справа (по умолчанию {COPY})")
    p.add_argument("--only", nargs="+", metavar="ID", help="только эти кадры")
    args = p.parse_args()

    before_dump, before = _snapshot(args.before)
    after_dump, after = _snapshot(args.after)
    print(f"слева  «{before_dump['name']}», {before_dump['lastModified']}, кадров {len(before)}")
    print(f"справа «{after_dump['name']}», {after_dump['lastModified']}, кадров {len(after)}")
    print()

    shared = set(before) & set(after)
    if args.only:
        shared &= set(args.only)

    changed = 0
    for frame_id in sorted(shared, key=_order):
        (left_name, left), (right_name, right) = before[frame_id], after[frame_id]
        if left == right and left_name == right_name:
            continue
        changed += 1
        title = f"{frame_id} | {left_name}"
        if left_name != right_name:
            title += f" -> {right_name}"
        print("=" * 70)
        print(title)
        for line in difflib.unified_diff(left, right, "слева", "справа", lineterm="", n=1):
            print(line)

    print()
    print(f"кадров разошлось: {changed}", file=sys.stderr)
    only_after = sorted(set(after) - set(before), key=_order)
    only_before = sorted(set(before) - set(after), key=_order)
    if only_after:
        print(f"только справа (сверить нечем): {', '.join(only_after)}", file=sys.stderr)
    if only_before:
        print(f"только слева (пропали): {', '.join(only_before)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
