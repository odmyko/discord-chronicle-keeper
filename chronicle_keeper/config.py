from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    asr_backend: str
    asr_language: str
    asr_dtype: str
    asr_max_new_tokens: int
    discord_bot_token: str
    qwen_asr_model: str
    qwen_asr_dtype: str
    qwen_asr_attn_implementation: str
    qwen_asr_max_new_tokens: int
    qwen_asr_max_inference_batch_size: int
    qwen_asr_warmup_on_start: bool
    vibevoice_python: str
    vibevoice_script: str
    vibevoice_model: str
    vibevoice_dtype: str
    vibevoice_max_new_tokens: int
    vibevoice_warmup_on_start: bool
    llm_base_url: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_chronicle_min_words: int
    llm_chronicle_max_words: int
    llm_warmup_on_start: bool
    summary_context_relevance_gate: bool
    summary_context_min_relevance: float
    lmstudio_auto_load: bool
    lmstudio_control_base_url: str
    lmstudio_control_load_path: str
    lmstudio_control_timeout_seconds: float
    lmstudio_auto_load_wait_seconds: float
    processing_timeout_seconds: int
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
    voice_decode_burst_window_seconds: int
    voice_decode_burst_threshold: int
    voice_decode_burst_cooldown_seconds: int
    data_dir: Path


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _derive_lmstudio_control_base_url(llm_base_url: str) -> str:
    base = (llm_base_url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        return base[: -len("/v1")]
    return base


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required in environment.")

    llm_base_url = os.getenv("LLM_BASE_URL", "").strip()
    llm_model = os.getenv("LLM_MODEL", "").strip()
    model_runner_base_url = os.getenv("MODEL_RUNNER_BASE_URL", "").strip()
    model_runner_name = os.getenv("MODEL_RUNNER_MODEL", "").strip()
    resolved_llm_base_url = (
        llm_base_url or model_runner_base_url or "http://127.0.0.1:1234/v1"
    ).strip()
    resolved_llm_model = (llm_model or model_runner_name or "local-model").strip()

    lmstudio_control_base_url = os.getenv(
        "LMSTUDIO_CONTROL_BASE_URL", ""
    ).strip().rstrip("/") or _derive_lmstudio_control_base_url(resolved_llm_base_url)
    lmstudio_control_load_path = os.getenv(
        "LMSTUDIO_CONTROL_LOAD_PATH", "/api/v1/models/load"
    ).strip()
    if not lmstudio_control_load_path.startswith("/"):
        lmstudio_control_load_path = f"/{lmstudio_control_load_path}"

    audio_mp3_vbr_quality = int(os.getenv("AUDIO_MP3_VBR_QUALITY", "4"))
    if audio_mp3_vbr_quality < 0:
        audio_mp3_vbr_quality = 0
    if audio_mp3_vbr_quality > 9:
        audio_mp3_vbr_quality = 9

    llm_chronicle_min_words = max(80, int(os.getenv("LLM_CHRONICLE_MIN_WORDS", "180")))
    llm_chronicle_max_words = max(120, int(os.getenv("LLM_CHRONICLE_MAX_WORDS", "320")))
    if llm_chronicle_max_words < llm_chronicle_min_words:
        llm_chronicle_max_words = llm_chronicle_min_words

    resolved_asr_language = (
        os.getenv("ASR_LANGUAGE", "").strip() or os.getenv("WHISPER_LANGUAGE", "ru")
    ).strip()

    return Settings(
        asr_backend=os.getenv("ASR_BACKEND", "qwen3_asr").strip().lower(),
        asr_language=resolved_asr_language,
        asr_dtype=os.getenv("ASR_DTYPE", "float16").strip().lower(),
        asr_max_new_tokens=max(128, int(os.getenv("ASR_MAX_NEW_TOKENS", "4096"))),
        discord_bot_token=token,
        qwen_asr_model=os.getenv("QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-1.7B").strip(),
        qwen_asr_dtype=(
            os.getenv("QWEN3_ASR_DTYPE", "").strip().lower()
            or os.getenv("ASR_DTYPE", "float16").strip().lower()
        ),
        qwen_asr_attn_implementation=os.getenv("QWEN3_ASR_ATTN_IMPLEMENTATION", "auto")
        .strip()
        .lower(),
        qwen_asr_max_new_tokens=max(
            128,
            int(
                (os.getenv("QWEN3_ASR_MAX_NEW_TOKENS", "").strip())
                or os.getenv("ASR_MAX_NEW_TOKENS", "4096")
            ),
        ),
        qwen_asr_max_inference_batch_size=max(
            1, int(os.getenv("QWEN3_ASR_MAX_INFERENCE_BATCH_SIZE", "32"))
        ),
        qwen_asr_warmup_on_start=_as_bool(
            os.getenv("QWEN3_ASR_WARMUP_ON_START", "false"), default=False
        ),
        vibevoice_python=os.getenv(
            "VIBEVOICE_PYTHON", r".\.venv-vibe\Scripts\python.exe"
        ).strip(),
        vibevoice_script=os.getenv(
            "VIBEVOICE_SCRIPT", "scripts/test_vibevoice_asr.py"
        ).strip(),
        vibevoice_model=os.getenv(
            "VIBEVOICE_MODEL", "microsoft/VibeVoice-ASR-HF"
        ).strip(),
        vibevoice_dtype=(
            os.getenv("VIBEVOICE_DTYPE", "").strip().lower()
            or os.getenv("ASR_DTYPE", "float16").strip().lower()
        ),
        vibevoice_max_new_tokens=max(
            256,
            int(
                (os.getenv("VIBEVOICE_MAX_NEW_TOKENS", "").strip())
                or os.getenv("ASR_MAX_NEW_TOKENS", "4096")
            ),
        ),
        vibevoice_warmup_on_start=_as_bool(
            os.getenv("VIBEVOICE_WARMUP_ON_START", "false"), default=False
        ),
        llm_base_url=resolved_llm_base_url.rstrip("/"),
        llm_model=resolved_llm_model,
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "1400")),
        llm_chronicle_min_words=llm_chronicle_min_words,
        llm_chronicle_max_words=llm_chronicle_max_words,
        llm_warmup_on_start=_as_bool(
            os.getenv("LLM_WARMUP_ON_START", "false"), default=False
        ),
        summary_context_relevance_gate=_as_bool(
            os.getenv("SUMMARY_CONTEXT_RELEVANCE_GATE", "false"), default=False
        ),
        summary_context_min_relevance=min(
            1.0, max(0.0, float(os.getenv("SUMMARY_CONTEXT_MIN_RELEVANCE", "0.40")))
        ),
        lmstudio_auto_load=_as_bool(
            os.getenv("LMSTUDIO_AUTO_LOAD", "false"), default=False
        ),
        lmstudio_control_base_url=lmstudio_control_base_url,
        lmstudio_control_load_path=lmstudio_control_load_path,
        lmstudio_control_timeout_seconds=max(
            1.0, float(os.getenv("LMSTUDIO_CONTROL_TIMEOUT_SECONDS", "180"))
        ),
        lmstudio_auto_load_wait_seconds=max(
            0.0, float(os.getenv("LMSTUDIO_AUTO_LOAD_WAIT_SECONDS", "1.5"))
        ),
        processing_timeout_seconds=int(os.getenv("PROCESSING_TIMEOUT_SECONDS", "7200")),
        recording_rotation_seconds=int(os.getenv("RECORDING_ROTATION_SECONDS", "1800")),
        recovery_auto_post_partial=_as_bool(
            os.getenv("RECOVERY_AUTO_POST_PARTIAL", "true"), default=True
        ),
        recovery_max_sessions=int(os.getenv("RECOVERY_MAX_SESSIONS", "20")),
        auto_cleanup_enabled=_as_bool(
            os.getenv("AUTO_CLEANUP_ENABLED", "false"), default=False
        ),
        auto_cleanup_on_start=_as_bool(
            os.getenv("AUTO_CLEANUP_ON_START", "false"), default=False
        ),
        retention_days=int(os.getenv("RETENTION_DAYS", "30")),
        allow_purge_commands=_as_bool(
            os.getenv("ALLOW_PURGE_COMMANDS", "false"), default=False
        ),
        audio_dual_pipeline_enabled=_as_bool(
            os.getenv("AUDIO_DUAL_PIPELINE_ENABLED", "false"), default=False
        ),
        audio_normalize=_as_bool(os.getenv("AUDIO_NORMALIZE", "false"), default=False),
        audio_vad_enabled=_as_bool(
            os.getenv("AUDIO_VAD_ENABLED", "false"), default=False
        ),
        audio_target_sample_rate=int(os.getenv("AUDIO_TARGET_SAMPLE_RATE", "0")),
        audio_target_channels=int(os.getenv("AUDIO_TARGET_CHANNELS", "0")),
        audio_mp3_vbr_quality=audio_mp3_vbr_quality,
        publish_per_speaker_audio=_as_bool(
            os.getenv("PUBLISH_PER_SPEAKER_AUDIO", "false"), default=False
        ),
        voice_decode_burst_window_seconds=max(
            5, int(os.getenv("VOICE_DECODE_BURST_WINDOW_SECONDS", "15"))
        ),
        voice_decode_burst_threshold=max(
            1, int(os.getenv("VOICE_DECODE_BURST_THRESHOLD", "8"))
        ),
        voice_decode_burst_cooldown_seconds=max(
            5, int(os.getenv("VOICE_DECODE_BURST_COOLDOWN_SECONDS", "60"))
        ),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
    )


def config_doctor_issues(settings: Settings) -> list[str]:
    issues: list[str] = []
    if settings.asr_backend not in {"qwen3_asr", "vibevoice_asr"}:
        issues.append("ASR_BACKEND must be one of: qwen3_asr, vibevoice_asr.")
    if settings.qwen_asr_dtype not in {"auto", "bfloat16", "float16", "float32"}:
        issues.append(
            "QWEN3_ASR_DTYPE must be one of: auto, bfloat16, float16, float32."
        )
    if settings.qwen_asr_attn_implementation not in {
        "auto",
        "eager",
        "sdpa",
        "flash_attention_2",
    }:
        issues.append(
            "QWEN3_ASR_ATTN_IMPLEMENTATION must be one of: auto, eager, sdpa, flash_attention_2."
        )
    if settings.asr_dtype not in {"auto", "bfloat16", "float16", "float32"}:
        issues.append("ASR_DTYPE must be one of: auto, bfloat16, float16, float32.")
    if settings.lmstudio_auto_load and not settings.lmstudio_control_base_url:
        issues.append("LMSTUDIO_AUTO_LOAD=true but LMSTUDIO_CONTROL_BASE_URL is empty.")
    return issues
