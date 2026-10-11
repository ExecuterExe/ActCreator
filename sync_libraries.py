"""Обновить копию библиотек внутри сайта: 08_Библиотеки → site/libraries.

Запуск (из папки site):  .venv\\Scripts\\python sync_libraries.py
Запускать после каждой правки библиотек и перед коммитом — иначе на хостинг уедет старая версия.
"""

import shutil
import sys

from library import BUNDLED_DIR, PROJECT_DIR


def main():
    if not PROJECT_DIR.is_dir():
        print(f"Не найдена папка с оригиналом библиотек: {PROJECT_DIR}")
        return 1
    BUNDLED_DIR.mkdir(exist_ok=True)
    source = {p.name: p for p in PROJECT_DIR.glob("*.md")}
    removed = [p for p in BUNDLED_DIR.glob("*.md") if p.name not in source]
    for p in removed:
        p.unlink()
    changed = 0
    for name, path in source.items():
        target = BUNDLED_DIR / name
        if not target.exists() or target.read_bytes() != path.read_bytes():
            shutil.copy2(path, target)
            changed += 1
    print(f"Библиотеки: {len(source)} файлов, обновлено {changed}, удалено устаревших {len(removed)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
