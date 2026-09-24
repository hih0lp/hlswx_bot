#!/usr/bin/env python3
"""Сверка экрана с кадром макета: построчно, вместе с пустыми строками.

`figma_diff.py` отвечает на вопрос «есть ли такая строка в боте вообще»,
а этот скрипт — на вопрос «так ли устроен конкретный экран»: сколько строк,
где пустые, в каком порядке. Отступы иначе не поймать.

Пары «кадр → константа» не ведём руками: в `app/core/admin_texts.py` номера
кадров уже стоят комментариями над константами, отсюда их и берём. Значит
таблица не протухнет — она и есть комментарии.

    PYTHONPATH=. ./scripts/figma_screens.py            # только расхождения
    PYTHONPATH=. ./scripts/figma_screens.py -v         # все экраны
    PYTHONPATH=. ./scripts/figma_screens.py -n QUEUE   # по имени константы

По умолчанию сверяемся с рабочей копией. Когда спор идёт о том, что нарисовано
у заказчика, а не у нас, тот же разбор нужен по оригиналу — для этого `--dump`:

    PYTHONPATH=. ./scripts/figma_screens.py --dump docs/design/figma.json.0907.bak

Кадры, которых в выбранном дампе нет, перечисляются в конце: снимок оригинала
старше клона, и экранов «Города и чаты» и «Тарифы» в нём ещё не было.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from figma_read import frames_of, render  # noqa: E402

DUMP = "docs/design/figma.json"
SOURCES = ("app/core/admin_texts.py", "app/core/texts.py")

TAG = re.compile(r"</?[a-z][a-z0-9]*>")
FRAME_ID = re.compile(r"\b(\d+:\d+)\b")
CONST = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=")


def mapping(path: str) -> list[tuple[list[str], str]]:
    """Кадры из комментария -> имя константы под ним."""
    pairs, pending = [], []
    with open(path) as handle:
        for line in handle:
            ids = FRAME_ID.findall(line)
            if line.lstrip().startswith("#"):
                if ids:
                    pending = ids
                continue
            match = CONST.match(line)
            if match:
                if pending:
                    pairs.append((pending, match.group(1)))
                pending = []
            elif line.strip() and not line.startswith(" "):
                pending = []
    return pairs


def bot_lines(value: str) -> list[str]:
    """Строки константы: HTML снят, пустые строки сохранены как есть."""
    return [TAG.sub("", line).strip() for line in value.split("\n")]


def design_lines(frame) -> list[str]:
    for kind, payload in render(frame):
        if kind == "text":
            return list(payload)
    return []


def shape(lines: list[str]) -> str:
    """Скелет экрана: где текст, где пустая строка. Точные слова не важны."""
    return "".join("_" if line.strip() else " " for line in lines)


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true", help="показывать совпавшие")
    p.add_argument("-n", "--names", nargs="+", help="только эти константы")
    p.add_argument("--dump", default=DUMP, help=f"дамп макета (по умолчанию {DUMP})")
    args = p.parse_args()

    if not os.path.exists(args.dump):
        sys.exit(f"нет {args.dump} — сначала выгрузите дамп")
    with open(args.dump) as handle:
        dump = json.load(handle)
    frames = {f["id"]: f for f in frames_of(dump)}
    print(f"# {args.dump}: «{dump['name']}», {dump['lastModified']}", file=sys.stderr)
    absent: list[str] = []

    import app.core.admin_texts as admin_texts
    import app.core.texts as texts

    modules = {"app/core/admin_texts.py": admin_texts, "app/core/texts.py": texts}

    bad = 0
    for path, module in modules.items():
        for ids, name in mapping(path):
            if args.names and name not in args.names:
                continue
            value = getattr(module, name, None)
            if not isinstance(value, str) or "\n" not in value:
                continue                      # подписи кнопок сверять нечего
            bot = bot_lines(value)
            for fid in ids:
                frame = frames.get(fid)
                if frame is None:
                    absent.append(f"{name} — {fid}")
                    continue
                design = design_lines(frame)
                same = shape(design) == shape(bot)
                if same and not args.verbose:
                    continue
                bad += 0 if same else 1
                mark = "совпадает" if same else "РАСХОЖДЕНИЕ"
                print(f"=== {name}  кадр {fid} ({frame['name']}) — {mark}")
                width = max((len(x) for x in design), default=0)
                for i in range(max(len(design), len(bot))):
                    left = design[i] if i < len(design) else ""
                    right = bot[i] if i < len(bot) else ""
                    flag = " " if left.strip() == right.strip() or (
                        bool(left.strip()) == bool(right.strip())) else "!"
                    print(f" {flag} {left[:width].ljust(min(width, 52))} | {right[:52]}")
                print()
    print(f"экранов с расхождением структуры: {bad}", file=sys.stderr)
    if absent:
        print(f"кадров нет в этом дампе, сверить нечем: {len(absent)}", file=sys.stderr)
        for item in absent:
            print(f"    {item}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
