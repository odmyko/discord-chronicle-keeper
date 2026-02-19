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
    processing_timeout_seconds: int
    summary_chunk_chars: int
    recording_rotation_seconds: int
    recovery_auto_post_partial: bool
    recovery_max_sessions: int
    audio_normalize: bool
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

    # Compose `models` can inject endpoint/model env vars.
    # Prefer generic LLM_* names, then legacy aliases, then LM Studio vars.
    llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
    llm_model = os.getenv("LLM_MODEL", "").strip()
    model_runner_base_url = os.getenv("MODEL_RUNNER_BASE_URL", "").strip()
    model_runner_name = os.getenv("MODEL_RUNNER_MODEL", "").strip()
    lmstudio_base_url = os.getenv("LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1").strip()
    lmstudio_model = os.getenv("LMSTUDIO_MODEL", "local-model").strip()

    return Settings(
        discord_bot_token=token,
        whisper_base_url=os.getenv("WHISPER_BASE_URL", "http://127.0.0.1:9000").rstrip("/"),
        whisper_asr_path=os.getenv("WHISPER_ASR_PATH", "/asr"),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "ru"),
        whisper_task=os.getenv("WHISPER_TASK", "transcribe"),
        whisper_encode=_as_bool(os.getenv("WHISPER_ENCODE", "true"), default=True),
        lmstudio_base_url=(llm_base_url or model_runner_base_url or lmstudio_base_url).rstrip("/"),
        lmstudio_model=llm_model or model_runner_name or lmstudio_model,
        lmstudio_temperature=float(os.getenv("LMSTUDIO_TEMPERATURE", "0.2")),
        lmstudio_max_tokens=int(os.getenv("LMSTUDIO_MAX_TOKENS", "1400")),
        processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "7200")),
        summary_chunk_chars=int(os.getenv("SUMMARY_CHUNK_CHARS", "14000")),
        recording_rotation_seconds=int(os.getenv("RECORDING_ROTATION_SECONDS", "1800")),
        recovery_auto_post_partial=_as_bool(os.getenv("RECOVERY_AUTO_POST_PARTIAL", "true"), default=True),
        recovery_max_sessions=int(os.getenv("RECOVERY_MAX_SESSIONS", "20")),
        audio_normalize=_as_bool(os.getenv("AUDIO_NORMALIZE", "false"), default=False),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
    )
