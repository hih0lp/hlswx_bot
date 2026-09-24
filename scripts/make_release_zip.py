"""Собрать полный ZIP для владельца (всё включено, кроме кэша)."""
from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parents[2] / "БООООТ" / "hwls-bot-full.zip"

SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}
SKIP_FILES: set[str] = set()


def should_include(rel: Path) -> bool:
    if rel.name in SKIP_FILES:
        return False
    if rel.suffix in {".pyc", ".pyo"}:
        return False
    for part in rel.parts:
        if part in SKIP_DIRS:
            return False
    return True


def main() -> None:
    if OUT.exists():
        OUT.unlink()
    count = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in ROOT.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(ROOT)
            if not should_include(rel):
                continue
            arc = str(rel).replace("\\", "/")
            zf.write(item, arc)
            count += 1
    size_kb = OUT.stat().st_size // 1024
    print(f"Created: {OUT}")
    print(f"Files: {count}, size: {size_kb} KB")


if __name__ == "__main__":
    main()
