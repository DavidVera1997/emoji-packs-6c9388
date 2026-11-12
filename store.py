"""Persistencia mínima en JSON: qué canal tiene qué pack de emojis."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any


class Store:
    """Guarda un registro por canal: set_name, sticker_file_id, photo_unique_id, owner_id."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        # Se crea en el primer uso para no requerir un event loop al instanciar
        # (asyncio.Lock() sin loop falla en Python 3.9).
        self._lock: asyncio.Lock | None = None
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._data.get(str(chat_id))

    def all(self) -> dict[str, dict[str, Any]]:
        return dict(self._data)

    async def put(self, chat_id: int, record: dict[str, Any]) -> None:
        async with self._get_lock():
            self._data[str(chat_id)] = record
            self._flush()

    async def delete(self, chat_id: int) -> None:
        async with self._get_lock():
            if self._data.pop(str(chat_id), None) is not None:
                self._flush()
