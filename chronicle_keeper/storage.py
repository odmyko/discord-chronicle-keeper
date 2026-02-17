from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class GuildSettingsStore:
    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._file_path.exists():
            self._write({"guilds": {}})

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"guilds": {}}

    def _write(self, payload: dict[str, Any]) -> None:
        self._file_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def set_chronicle_channel(self, guild_id: int, channel_id: int) -> None:
        payload = self._read()
        guilds = payload.setdefault("guilds", {})
        guild_cfg = guilds.setdefault(str(guild_id), {})
        guild_cfg["chronicle_channel_id"] = channel_id
        self._write(payload)

    def get_chronicle_channel(self, guild_id: int) -> int | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        channel_id = guild_cfg.get("chronicle_channel_id")
        return int(channel_id) if channel_id is not None else None

    def set_voice_channel(self, guild_id: int, channel_id: int) -> None:
        payload = self._read()
        guilds = payload.setdefault("guilds", {})
        guild_cfg = guilds.setdefault(str(guild_id), {})
        guild_cfg["voice_channel_id"] = channel_id
        self._write(payload)

    def get_voice_channel(self, guild_id: int) -> int | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        channel_id = guild_cfg.get("voice_channel_id")
        return int(channel_id) if channel_id is not None else None

    def set_summary_language(self, guild_id: int, language: str) -> None:
        payload = self._read()
        guilds = payload.setdefault("guilds", {})
        guild_cfg = guilds.setdefault(str(guild_id), {})
        guild_cfg["summary_language"] = language.lower().strip()
        self._write(payload)

    def get_summary_language(self, guild_id: int, default: str = "ru") -> str:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        value = guild_cfg.get("summary_language", default)
        if not isinstance(value, str) or not value.strip():
            return default
        return value.lower().strip()
