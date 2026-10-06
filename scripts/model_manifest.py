"""Создание и проверка контрольных сумм офлайн-моделей."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def inventory(root: Path) -> list[dict]:
    if not root.is_dir():
        raise ValueError(f"Каталог не найден: {root}")
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Символическая ссылка запрещена: {path}")
        if path.is_file():
            result.append({"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": file_hash(path)})
    if not result:
        raise ValueError("Каталог пуст")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["create", "verify"])
    parser.add_argument("root", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    current = inventory(args.root)
    if args.mode == "create":
        args.manifest.write_text(json.dumps({"files": current}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Записано файлов: {len(current)}")
    else:
        expected = json.loads(args.manifest.read_text(encoding="utf-8"))["files"]
        if current != expected:
            raise SystemExit("Контрольные суммы, размеры или состав пакета не совпадают")
        print(f"Пакет проверен: {len(current)} файлов")


if __name__ == "__main__":
    main()
