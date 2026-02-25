# Config Reference

This project is configured through `.env`.

## Core

- `DISCORD_BOT_TOKEN`: Discord bot token (required)
- `DATA_DIR`: artifacts root (default `./data`)

## Whisper (Primary)

- `BOT_WHISPER_BASE_URL`: bot-side ASR base URL in compose network
- `WHISPER_API_STYLE`: `asr` or `openai`
- `WHISPER_ASR_PATH`: endpoint path (`/asr` or `/v1/audio/transcriptions`)
- `WHISPER_LANGUAGE`: language hint (`ru`, `uk`, `en`, etc.)
- `WHISPER_TASK`: `transcribe`
- `WHISPER_ENCODE`: `true|false`
- `WHISPER_OPENAI_MODEL`: model id for openai-style endpoint
- `WHISPER_OPENAI_TEMPERATURE`: decoding temperature
- `WHISPER_OPENAI_PROMPT`: optional names/lore hint
- `WHISPER_WARMUP_ON_START`: pre-warm ASR at bot startup

## Whisper Fallback

- `WHISPER_FALLBACK_ENABLED`: enable secondary ASR endpoint retry
- `WHISPER_FALLBACK_BASE_URL`: fallback ASR base URL
- `WHISPER_FALLBACK_API_STYLE`: fallback style (`asr|openai`)
- `WHISPER_FALLBACK_ASR_PATH`: fallback endpoint path
- `WHISPER_FALLBACK_OPENAI_MODEL`: fallback model for openai style

Quality-gate fallback:

- `WHISPER_FALLBACK_ON_LOW_QUALITY`: retry on weak primary transcript
- `WHISPER_LOW_QUALITY_MIN_CHARS`: minimum text chars threshold
- `WHISPER_LOW_QUALITY_MIN_SEGMENTS`: minimum segments threshold

## LLM

- `LLM_BASE_URL`: OpenAI-compatible base URL
- `LLM_MODEL`: model id
- `LLM_TEMPERATURE`
- `LLM_MAX_TOKENS`
- `LLM_WARMUP_ON_START`

## Audio Processing

- `AUDIO_DUAL_PIPELINE_ENABLED`
- `AUDIO_NORMALIZE`
- `AUDIO_VAD_ENABLED`
- `AUDIO_TARGET_CHANNELS`
- `AUDIO_TARGET_SAMPLE_RATE`
- `AUDIO_MP3_VBR_QUALITY`
- `PUBLISH_PER_SPEAKER_AUDIO`

## Session/Operations

- `PROCESSING_TIMEOUT_SECONDS`
- `SUMMARY_CHUNK_CHARS`
- `RECORDING_ROTATION_SECONDS`
- `RECOVERY_AUTO_POST_PARTIAL`
- `RECOVERY_MAX_SESSIONS`
- `VOICE_DECODE_BURST_WINDOW_SECONDS`
- `VOICE_DECODE_BURST_THRESHOLD`
- `VOICE_DECODE_BURST_COOLDOWN_SECONDS`

## Retention/Safety

- `AUTO_CLEANUP_ENABLED`
- `AUTO_CLEANUP_ON_START`
- `RETENTION_DAYS`
- `ALLOW_PURGE_COMMANDS`

## Backend Presets

### Classic `/asr`

- `BOT_WHISPER_BASE_URL=http://whisper:9000`
- `WHISPER_API_STYLE=asr`
- `WHISPER_ASR_PATH=/asr`

### vLLM OpenAI

- `BOT_WHISPER_BASE_URL=http://whisper_vllm:8000`
- `WHISPER_API_STYLE=openai`
- `WHISPER_ASR_PATH=/v1/audio/transcriptions`
