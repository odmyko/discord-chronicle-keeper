# Config Reference

This project is configured via `.env`.

## Core

- `DISCORD_BOT_TOKEN`: Discord bot token (required)
- `DATA_DIR`: artifacts root (default `./data`)

## ASR backend selection

- `ASR_BACKEND`: `qwen3_asr` (default) or `vibevoice_asr`
- `ASR_LANGUAGE`: language hint (`ru|uk|en` or full language name)
- `ASR_DTYPE`: `auto|bfloat16|float16|float32` (shared default)
- `ASR_MAX_NEW_TOKENS`: shared default output cap

## Qwen3-ASR options

- `QWEN3_ASR_MODEL`: model id (default `Qwen/Qwen3-ASR-1.7B`)
- `QWEN3_ASR_DTYPE`: optional override (fallback: `ASR_DTYPE`)
- `QWEN3_ASR_ATTN_IMPLEMENTATION`: `auto|eager|sdpa|flash_attention_2`
- `QWEN3_ASR_MAX_NEW_TOKENS`: optional override (fallback: `ASR_MAX_NEW_TOKENS`)
- `QWEN3_ASR_MAX_INFERENCE_BATCH_SIZE`: inference batch sizing
- `QWEN3_ASR_WARMUP_ON_START`: warm ASR model on startup

## VibeVoice-ASR options

- `VIBEVOICE_PYTHON`: Python interpreter path for separate env (default `.\\.venv-vibe\\Scripts\\python.exe`)
- `VIBEVOICE_SCRIPT`: runner script path (default `scripts/test_vibevoice_asr.py`)
- `VIBEVOICE_MODEL`: model id (default `microsoft/VibeVoice-ASR-HF`)
- `VIBEVOICE_DTYPE`: optional override (fallback: `ASR_DTYPE`)
- `VIBEVOICE_MAX_NEW_TOKENS`: optional override (fallback: `ASR_MAX_NEW_TOKENS`)
- `VIBEVOICE_WARMUP_ON_START`: run startup warmup

## LLM

- `LLM_BASE_URL`: OpenAI-compatible base URL
- `LLM_MODEL`: model id
- `LLM_TEMPERATURE`
- `LLM_MAX_TOKENS`
- `LLM_CHRONICLE_MIN_WORDS`: lower target for Player-Facing Chronicle Post length
- `LLM_CHRONICLE_MAX_WORDS`: upper target for Player-Facing Chronicle Post length
- `LLM_WARMUP_ON_START`

### LM Studio auto-load (optional)

- `LMSTUDIO_AUTO_LOAD`
- `LMSTUDIO_CONTROL_BASE_URL`
- `LMSTUDIO_CONTROL_LOAD_PATH`
- `LMSTUDIO_CONTROL_TIMEOUT_SECONDS`
- `LMSTUDIO_AUTO_LOAD_WAIT_SECONDS`

## Summary and processing

- `PROCESSING_TIMEOUT_SECONDS`
- `SUMMARY_CONTEXT_RELEVANCE_GATE`
- `SUMMARY_CONTEXT_MIN_RELEVANCE`

## Recording and reliability

- `RECORDING_ROTATION_SECONDS`
- `VOICE_PATCH_MODE`
- `RECOVERY_AUTO_POST_PARTIAL`
- `RECOVERY_MAX_SESSIONS`
- `VOICE_DECODE_BURST_WINDOW_SECONDS`
- `VOICE_DECODE_BURST_THRESHOLD`
- `VOICE_DECODE_BURST_COOLDOWN_SECONDS`

## Voice sidecar (optional)

- `VOICE_SIDECAR_ENABLED`: if `true`, `/chronicle_start` and `/chronicle_stop` use sidecar API instead of Python voice receive
- `VOICE_SIDECAR_BASE_URL`: sidecar base URL (default `http://127.0.0.1:8081`)
- `VOICE_SIDECAR_TIMEOUT_SECONDS`: sidecar request timeout
- `SIDECAR_TOKEN`: optional auth token sent as `X-Sidecar-Token`
- `SIDECAR_DAVE_ENCRYPTION`: enable DAVE/encryption mode in sidecar runtime
- `SIDECAR_DECRYPTION_FAILURE_TOLERANCE`: tolerated decrypt errors before stream reset

## Audio processing

- `AUDIO_DUAL_PIPELINE_ENABLED`
- `AUDIO_NORMALIZE`
- `AUDIO_VAD_ENABLED`
- `AUDIO_TARGET_CHANNELS`
- `AUDIO_TARGET_SAMPLE_RATE`
- `AUDIO_MP3_VBR_QUALITY`
- `PUBLISH_PER_SPEAKER_AUDIO`

## Retention and safety

- `AUTO_CLEANUP_ENABLED`
- `AUTO_CLEANUP_ON_START`
- `RETENTION_DAYS`
- `ALLOW_PURGE_COMMANDS`

## Legacy compatibility

`WHISPER_LANGUAGE` is still accepted as fallback for `ASR_LANGUAGE`.
