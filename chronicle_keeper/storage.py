from __future__ import annotations

from datetime import datetime, UTC
import json
from pathlib import Path
import uuid
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
        self._file_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _new_campaign_id() -> str:
        return uuid.uuid4().hex[:10]

    @staticmethod
    def _normalize_language(value: str, default: str = "ru") -> str:
        clean = (value or "").strip().lower()
        return clean if clean in {"en", "uk", "ru"} else default

    def _ensure_guild_cfg(
        self, payload: dict[str, Any], guild_id: int
    ) -> dict[str, Any]:
        guilds = payload.setdefault("guilds", {})
        guild_cfg = guilds.setdefault(str(guild_id), {})
        guild_cfg.setdefault("default_summary_language", "ru")
        guild_cfg.setdefault("default_session_context", "")
        guild_cfg.setdefault("default_name_hints", "")
        guild_cfg.setdefault("campaigns", {})
        return guild_cfg

    def set_chronicle_channel(self, guild_id: int, channel_id: int) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        guild_cfg["chronicle_channel_id"] = channel_id
        self._write(payload)

    def get_chronicle_channel(self, guild_id: int) -> int | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        channel_id = guild_cfg.get("chronicle_channel_id")
        return int(channel_id) if channel_id is not None else None

    def set_voice_channel(self, guild_id: int, channel_id: int) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        guild_cfg["voice_channel_id"] = channel_id
        self._write(payload)

    def get_voice_channel(self, guild_id: int) -> int | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        channel_id = guild_cfg.get("voice_channel_id")
        return int(channel_id) if channel_id is not None else None

    # Guild defaults
    def set_default_summary_language(self, guild_id: int, language: str) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        guild_cfg["default_summary_language"] = self._normalize_language(
            language, default="ru"
        )
        self._write(payload)

    def get_default_summary_language(self, guild_id: int, default: str = "ru") -> str:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        value = guild_cfg.get("default_summary_language", default)
        if not isinstance(value, str) or not value.strip():
            return default
        return self._normalize_language(value, default=default)

    def set_default_session_context(self, guild_id: int, context: str) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        guild_cfg["default_session_context"] = context.strip()
        self._write(payload)

    def get_default_session_context(self, guild_id: int, default: str = "") -> str:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        value = guild_cfg.get("default_session_context", default)
        return value.strip() if isinstance(value, str) else default

    def set_default_name_hints(self, guild_id: int, hints: str) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        guild_cfg["default_name_hints"] = hints.strip()
        self._write(payload)

    def get_default_name_hints(self, guild_id: int, default: str = "") -> str:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        value = guild_cfg.get("default_name_hints", default)
        return value.strip() if isinstance(value, str) else default

    # Campaign management
    def create_campaign(
        self,
        guild_id: int,
        name: str,
        summary_language: str = "",
    ) -> dict[str, Any]:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        campaigns = guild_cfg.setdefault("campaigns", {})

        clean_name = name.strip()
        if not clean_name:
            raise ValueError("Campaign name is empty.")
        for existing in campaigns.values():
            if (
                isinstance(existing, dict)
                and existing.get("name", "").strip().lower() == clean_name.lower()
            ):
                raise ValueError(f"Campaign '{clean_name}' already exists.")

        campaign_id = self._new_campaign_id()
        campaign = {
            "id": campaign_id,
            "name": clean_name,
            "summary_language": self._normalize_language(summary_language, default="ru")
            if summary_language.strip()
            else "",
            "session_context": "",
            "name_hints": "",
            "created_at_utc": datetime.now(UTC).isoformat(),
        }
        campaigns[campaign_id] = campaign
        if not guild_cfg.get("active_campaign_id"):
            guild_cfg["active_campaign_id"] = campaign_id
        self._write(payload)
        return campaign

    def list_campaigns(self, guild_id: int) -> list[dict[str, Any]]:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        campaigns = guild_cfg.get("campaigns", {})
        if not isinstance(campaigns, dict):
            return []
        rows: list[dict[str, Any]] = []
        for value in campaigns.values():
            if isinstance(value, dict):
                rows.append(dict(value))
        rows.sort(key=lambda x: (x.get("name") or "").lower())
        return rows

    def get_campaign(self, guild_id: int, campaign_id: str) -> dict[str, Any] | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        campaigns = guild_cfg.get("campaigns", {})
        if not isinstance(campaigns, dict):
            return None
        value = campaigns.get(campaign_id)
        return dict(value) if isinstance(value, dict) else None

    def find_campaign(self, guild_id: int, campaign_ref: str) -> dict[str, Any] | None:
        campaign_ref = campaign_ref.strip()
        if not campaign_ref:
            return None
        exact = self.get_campaign(guild_id, campaign_ref)
        if exact is not None:
            return exact
        for campaign in self.list_campaigns(guild_id):
            if (campaign.get("name") or "").strip().lower() == campaign_ref.lower():
                return campaign
        return None

    def set_active_campaign(self, guild_id: int, campaign_id: str) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        campaigns = guild_cfg.get("campaigns", {})
        if not isinstance(campaigns, dict) or campaign_id not in campaigns:
            raise ValueError(f"Campaign id '{campaign_id}' does not exist.")
        guild_cfg["active_campaign_id"] = campaign_id
        self._write(payload)

    def get_active_campaign_id(self, guild_id: int) -> str | None:
        payload = self._read()
        guild_cfg = payload.get("guilds", {}).get(str(guild_id), {})
        value = guild_cfg.get("active_campaign_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def update_campaign(
        self,
        guild_id: int,
        campaign_id: str,
        *,
        name: str | None = None,
        summary_language: str | None = None,
        session_context: str | None = None,
        name_hints: str | None = None,
    ) -> dict[str, Any]:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        campaigns = guild_cfg.setdefault("campaigns", {})
        if campaign_id not in campaigns or not isinstance(campaigns[campaign_id], dict):
            raise ValueError(f"Campaign id '{campaign_id}' does not exist.")
        campaign = campaigns[campaign_id]

        if name is not None:
            clean_name = name.strip()
            if not clean_name:
                raise ValueError("Campaign name is empty.")
            campaign["name"] = clean_name
        if summary_language is not None:
            clean = summary_language.strip()
            campaign["summary_language"] = (
                self._normalize_language(clean, default="ru") if clean else ""
            )
        if session_context is not None:
            campaign["session_context"] = session_context.strip()
        if name_hints is not None:
            campaign["name_hints"] = name_hints.strip()
        self._write(payload)
        return dict(campaign)

    def delete_campaign(self, guild_id: int, campaign_id: str) -> None:
        payload = self._read()
        guild_cfg = self._ensure_guild_cfg(payload, guild_id)
        campaigns = guild_cfg.setdefault("campaigns", {})
        if campaign_id not in campaigns:
            raise ValueError(f"Campaign id '{campaign_id}' does not exist.")
        campaigns.pop(campaign_id, None)
        if guild_cfg.get("active_campaign_id") == campaign_id:
            guild_cfg["active_campaign_id"] = None
        self._write(payload)

    def resolve_active_campaign_settings(self, guild_id: int) -> dict[str, str]:
        default_lang = self.get_default_summary_language(guild_id, default="ru")
        default_context = self.get_default_session_context(guild_id, default="")
        default_hints = self.get_default_name_hints(guild_id, default="")
        active_campaign_id = self.get_active_campaign_id(guild_id)

        campaign_name = ""
        campaign_lang = ""
        campaign_context = ""
        campaign_hints = ""
        if active_campaign_id:
            campaign = self.get_campaign(guild_id, active_campaign_id)
            if campaign:
                campaign_name = str(campaign.get("name") or "").strip()
                campaign_lang = str(campaign.get("summary_language") or "").strip()
                campaign_context = str(campaign.get("session_context") or "").strip()
                campaign_hints = str(campaign.get("name_hints") or "").strip()

        return {
            "campaign_id": active_campaign_id or "",
            "campaign_name": campaign_name,
            "summary_language": campaign_lang or default_lang,
            "session_context": campaign_context or default_context,
            "name_hints": campaign_hints or default_hints,
        }

    # Legacy aliases kept as internal convenience wrappers.
    def set_summary_language(self, guild_id: int, language: str) -> None:
        self.set_default_summary_language(guild_id, language)

    def get_summary_language(self, guild_id: int, default: str = "ru") -> str:
        return self.get_default_summary_language(guild_id, default=default)
