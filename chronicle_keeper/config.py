from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    whisper_base_url: str
    whisper_asr_path: str
    whisper_language: str
    whisper_task: str
    whisper_encode: bool
    lmstudio_base_url: str
    lmstudio_model: str
    lmstudio_temperature: float
    lmstudio_max_tokens: int
    data_dir: Path


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in environment.")

    return Settings(
        discord_bot_token=token,
        whisper_base_url=os.getenv("WHISPER_BASE_URL", "http://127.0.0.1:9000").rstrip("/"),
        whisper_asr_path=os.getenv("WHISPER_ASR_PATH", "/asr"),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "ru"),
        whisper_task=os.getenv("WHISPER_TASK", "transcribe"),
        whisper_encode=_as_bool(os.getenv("WHISPER_ENCODE", "true"), default=True),
        lmstudio_base_url=os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").rstrip("/"),
        lmstudio_model=os.getenv("LMSTUDIO_MODEL", "local-model"),
        lmstudio_temperature=float(os.getenv("LMSTUDIO_TEMPERATURE", "0.2")),
        lmstudio_max_tokens=int(os.getenv("LMSTUDIO_MAX_TOKENS", "1400")),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
    )

