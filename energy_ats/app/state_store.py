"""Небольшой атомарный журнал состояния Energy Supervisor."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self.path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Файл состояния должен содержать JSON object")
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        """Записать новое состояние до выполнения физических команд."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

        # fsync файла гарантирует его содержимое, fsync каталога — сам факт
        # атомарного rename после внезапной потери питания хоста.
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
