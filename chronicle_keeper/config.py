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
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    processing_timeout_seconds: int
    summary_chunk_chars: int
    recording_rotation_seconds: int
    recovery_auto_post_partial: bool
    recovery_max_sessions: int
    auto_cleanup_enabled: bool
    auto_cleanup_on_start: bool
    retention_days: int
    allow_purge_commands: bool
    audio_normalize: bool
    audio_vad_enabled: bool
    audio_target_sample_rate: int
    audio_target_channels: int
    audio_mp3_vbr_quality: int
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
    # Prefer generic LLM_* names, then MODEL_RUNNER_* aliases.
    llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
    llm_model = os.getenv("LLM_MODEL", "").strip()
    model_runner_base_url = os.getenv("MODEL_RUNNER_BASE_URL", "").strip()
    model_runner_name = os.getenv("MODEL_RUNNER_MODEL", "").strip()
    resolved_llm_base_url = (llm_base_url or model_runner_base_url or "http://127.0.0.1:1234/v1").strip()
    resolved_llm_model = (llm_model or model_runner_name or "local-model").strip()
    audio_mp3_vbr_quality = int(os.getenv("AUDIO_MP3_VBR_QUALITY", "4"))
    if audio_mp3_vbr_quality < 0:
        audio_mp3_vbr_quality = 0
    if audio_mp3_vbr_quality > 9:
        audio_mp3_vbr_quality = 9

    return Settings(
        discord_bot_token=token,
        whisper_base_url=os.getenv("WHISPER_BASE_URL", "http://127.0.0.1:9000").rstrip("/"),
        whisper_asr_path=os.getenv("WHISPER_ASR_PATH", "/asr"),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "ru"),
        whisper_task=os.getenv("WHISPER_TASK", "transcribe"),
        whisper_encode=_as_bool(os.getenv("WHISPER_ENCODE", "true"), default=True),
        llm_base_url=resolved_llm_base_url.rstrip("/"),
        llm_model=resolved_llm_model,
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1400")),
        processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "7200")),
        summary_chunk_chars=int(os.getenv("SUMMARY_CHUNK_CHARS", "14000")),
        recording_rotation_seconds=int(os.getenv("RECORDING_ROTATION_SECONDS", "1800")),
        recovery_auto_post_partial=_as_bool(os.getenv("RECOVERY_AUTO_POST_PARTIAL", "true"), default=True),
        recovery_max_sessions=int(os.getenv("RECOVERY_MAX_SESSIONS", "20")),
        auto_cleanup_enabled=_as_bool(os.getenv("AUTO_CLEANUP_ENABLED", "false"), default=False),
        auto_cleanup_on_start=_as_bool(os.getenv("AUTO_CLEANUP_ON_START", "false"), default=False),
        retention_days=int(os.getenv("RETENTION_DAYS", "30")),
        allow_purge_commands=_as_bool(os.getenv("ALLOW_PURGE_COMMANDS", "false"), default=False),
        audio_normalize=_as_bool(os.getenv("AUDIO_NORMALIZE", "false"), default=False),
        audio_vad_enabled=_as_bool(os.getenv("AUDIO_VAD_ENABLED", "false"), default=False),
        audio_target_sample_rate=int(os.getenv("AUDIO_TARGET_SAMPLE_RATE", "0")),
        audio_target_channels=int(os.getenv("AUDIO_TARGET_CHANNELS", "0")),
        audio_mp3_vbr_quality=audio_mp3_vbr_quality,
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
    )
