from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    discord_bot_token: str
    whisper_base_url: str
    whisper_api_style: str
    whisper_asr_path: str
    whisper_openai_model: str
    whisper_openai_temperature: float
    whisper_openai_prompt: str
    whisper_language: str
    whisper_task: str
    whisper_encode: bool
    whisper_warmup_on_start: bool
    whisper_fallback_enabled: bool
    whisper_fallback_base_url: str
    whisper_fallback_api_style: str
    whisper_fallback_asr_path: str
    whisper_fallback_openai_model: str
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_warmup_on_start: bool
    processing_timeout_seconds: int
    summary_chunk_chars: int
    recording_rotation_seconds: int
    recovery_auto_post_partial: bool
    recovery_max_sessions: int
    auto_cleanup_enabled: bool
    auto_cleanup_on_start: bool
    retention_days: int
    allow_purge_commands: bool
    audio_dual_pipeline_enabled: bool
    audio_normalize: bool
    audio_vad_enabled: bool
    audio_target_sample_rate: int
    audio_target_channels: int
    audio_mp3_vbr_quality: int
    publish_per_speaker_audio: bool
    data_dir: Path


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_whisper_path(api_style: str, path_value: str) -> str:
    path = (path_value or "").strip()
    if path and not path.startswith("/"):
        path = f"/{path}"
    if not path:
        path = "/v1/audio/transcriptions" if api_style == "openai" else "/asr"
    if api_style == "openai" and path == "/asr":
        path = "/v1/audio/transcriptions"
    if api_style == "asr" and path == "/v1/audio/transcriptions":
        path = "/asr"
    return path


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

    whisper_api_style = os.getenv("WHISPER_API_STYLE", "asr").strip().lower()
    if whisper_api_style not in {"asr", "openai"}:
        whisper_api_style = "asr"
    whisper_fallback_api_style = os.getenv("WHISPER_FALLBACK_API_STYLE", "").strip().lower()
    if whisper_fallback_api_style and whisper_fallback_api_style not in {"asr", "openai"}:
        whisper_fallback_api_style = ""

    whisper_asr_path = _normalize_whisper_path(whisper_api_style, os.getenv("WHISPER_ASR_PATH", ""))
    fallback_style_resolved = whisper_fallback_api_style or whisper_api_style
    fallback_path_default = os.getenv("WHISPER_FALLBACK_ASR_PATH", "")
    whisper_fallback_asr_path = _normalize_whisper_path(fallback_style_resolved, fallback_path_default)
    whisper_fallback_base_url = os.getenv("WHISPER_FALLBACK_BASE_URL", "").strip().rstrip("/")
    whisper_fallback_enabled_default = bool(whisper_fallback_base_url)
    whisper_fallback_enabled = _as_bool(
        os.getenv("WHISPER_FALLBACK_ENABLED", "true" if whisper_fallback_enabled_default else "false"),
        default=whisper_fallback_enabled_default,
    )

    return Settings(
        discord_bot_token=token,
        whisper_base_url=os.getenv("WHISPER_BASE_URL", "http://127.0.0.1:9000").rstrip("/"),
        whisper_api_style=whisper_api_style,
        whisper_asr_path=whisper_asr_path,
        whisper_openai_model=os.getenv("WHISPER_OPENAI_MODEL", "openai/whisper-large-v3-turbo").strip(),
        whisper_openai_temperature=float(os.getenv("WHISPER_OPENAI_TEMPERATURE", "0.0")),
        whisper_openai_prompt=os.getenv("WHISPER_OPENAI_PROMPT", "").strip(),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "ru"),
        whisper_task=os.getenv("WHISPER_TASK", "transcribe"),
        whisper_encode=_as_bool(os.getenv("WHISPER_ENCODE", "true"), default=True),
        whisper_warmup_on_start=_as_bool(os.getenv("WHISPER_WARMUP_ON_START", "false"), default=False),
        whisper_fallback_enabled=whisper_fallback_enabled,
        whisper_fallback_base_url=whisper_fallback_base_url,
        whisper_fallback_api_style=fallback_style_resolved,
        whisper_fallback_asr_path=whisper_fallback_asr_path,
        whisper_fallback_openai_model=(
            os.getenv("WHISPER_FALLBACK_OPENAI_MODEL", "").strip()
            or os.getenv("WHISPER_OPENAI_MODEL", "openai/whisper-large-v3-turbo").strip()
        ),
        llm_base_url=resolved_llm_base_url.rstrip("/"),
        llm_model=resolved_llm_model,
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1400")),
        llm_warmup_on_start=_as_bool(os.getenv("LLM_WARMUP_ON_START", "false"), default=False),
        processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "7200")),
        summary_chunk_chars=int(os.getenv("SUMMARY_CHUNK_CHARS", "14000")),
        recording_rotation_seconds=int(os.getenv("RECORDING_ROTATION_SECONDS", "1800")),
        recovery_auto_post_partial=_as_bool(os.getenv("RECOVERY_AUTO_POST_PARTIAL", "true"), default=True),
        recovery_max_sessions=int(os.getenv("RECOVERY_MAX_SESSIONS", "20")),
        auto_cleanup_enabled=_as_bool(os.getenv("AUTO_CLEANUP_ENABLED", "false"), default=False),
        auto_cleanup_on_start=_as_bool(os.getenv("AUTO_CLEANUP_ON_START", "false"), default=False),
        retention_days=int(os.getenv("RETENTION_DAYS", "30")),
        allow_purge_commands=_as_bool(os.getenv("ALLOW_PURGE_COMMANDS", "false"), default=False),
        audio_dual_pipeline_enabled=_as_bool(os.getenv("AUDIO_DUAL_PIPELINE_ENABLED", "false"), default=False),
        audio_normalize=_as_bool(os.getenv("AUDIO_NORMALIZE", "false"), default=False),
        audio_vad_enabled=_as_bool(os.getenv("AUDIO_VAD_ENABLED", "false"), default=False),
        audio_target_sample_rate=int(os.getenv("AUDIO_TARGET_SAMPLE_RATE", "0")),
        audio_target_channels=int(os.getenv("AUDIO_TARGET_CHANNELS", "0")),
        audio_mp3_vbr_quality=audio_mp3_vbr_quality,
        publish_per_speaker_audio=_as_bool(os.getenv("PUBLISH_PER_SPEAKER_AUDIO", "false"), default=False),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
    )


def config_doctor_issues(settings: Settings) -> list[str]:
    issues: list[str] = []
    if settings.whisper_api_style == "asr" and settings.whisper_asr_path != "/asr":
        issues.append(
            f"WHISPER_API_STYLE=asr but WHISPER_ASR_PATH={settings.whisper_asr_path}. Expected /asr."
        )
    if settings.whisper_api_style == "openai" and settings.whisper_asr_path != "/v1/audio/transcriptions":
        issues.append(
            "WHISPER_API_STYLE=openai but WHISPER_ASR_PATH is not /v1/audio/transcriptions."
        )
    if settings.whisper_fallback_enabled and not settings.whisper_fallback_base_url:
        issues.append("WHISPER_FALLBACK_ENABLED=true but WHISPER_FALLBACK_BASE_URL is empty.")
    if settings.whisper_fallback_enabled:
        same_target = (
            settings.whisper_base_url.rstrip("/") == settings.whisper_fallback_base_url.rstrip("/")
            and settings.whisper_api_style == settings.whisper_fallback_api_style
            and settings.whisper_asr_path == settings.whisper_fallback_asr_path
        )
        if same_target:
            issues.append("Whisper fallback target matches primary target; fallback has no effect.")
    return issues
