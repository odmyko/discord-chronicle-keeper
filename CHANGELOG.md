# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project aims to follow Semantic Versioning.

## [Unreleased]

## [0.8.0] - 2026-02-21

### Added
- Startup config doctor checks with warnings for common ASR misconfiguration.
- Whisper fallback quality gate controls:
  - `WHISPER_FALLBACK_ON_LOW_QUALITY`
  - `WHISPER_LOW_QUALITY_MIN_CHARS`
  - `WHISPER_LOW_QUALITY_MIN_SEGMENTS`
- New utility scripts:
  - `scripts/smoke_e2e.py` for quick end-to-end ASR+LLM health checks
  - `scripts/switch_asr_backend.py` to switch `.env` between `asr` and `vllm` backends.

### Changed
- Whisper fallback now supports two triggers:
  - hard request failures
  - weak primary transcript quality (heuristic scoring and replacement with better fallback result).
- README and `.env.example` updated with fallback quality-gate and backend switch workflow.

## [0.7.0] - 2026-02-21

### Added
- OpenAI-style Whisper transcription support improvements:
  - configurable OpenAI transcription knobs (`WHISPER_OPENAI_TEMPERATURE`, `WHISPER_OPENAI_PROMPT`)
  - startup ASR warmup (`WHISPER_WARMUP_ON_START`)
- LLM startup warmup option (`LLM_WARMUP_ON_START`) to reduce first summary latency.
- Optional vLLM Whisper audio service image recipe:
  - `docker/vllm-whisper-audio/Dockerfile` with audio extras for `/v1/audio/transcriptions`.

### Changed
- Startup lifecycle now performs optional warmup calls for Whisper and LLM with structured logs.
- Docker Compose docs and examples updated for vLLM OpenAI transcription routing.
- Benchmark docs expanded for both `/asr` and OpenAI transcription endpoints.

### Fixed
- Guarded Whisper API style/path mismatch in config:
  - auto-corrects conflicting `WHISPER_API_STYLE` + `WHISPER_ASR_PATH` combinations
  - prevents common misconfiguration where OpenAI payload is sent to `/asr`.
- LLM warmup now uses a short timeout (fail-fast) to avoid blocking bot readiness.

## [0.6.1] - 2026-02-20

### Added
- `/chronicle_reprocess_last` slash command for one-click reprocessing of the latest saved guild session.
- Helper script for local Whisper CTranslate2 setup:
  - `scripts/prepare_whisper_ct2_model.py` converts model and updates `.env`.

### Changed
- Summary generation tone improved for DnD/TTRPG use:
  - still deterministic section structure
  - more narrative, player-facing chronicle style.
- Whisper CT2 helper hardened:
  - uses current Python interpreter
  - validates conversion dependencies
  - copies only available HF files (`tokenizer.json`, `preprocessor_config.json`)
  - validates generated artifacts and warns on missing preprocessor config.
- README updated with CT2 conversion prerequisites and troubleshooting guidance.

## [0.6.0] - 2026-02-20

### Added
- Recorder health and operational features:
  - `/chronicle_status` command with runtime counters and connection state
  - post-session recording quality report (duration/bitrate/sample-rate/reconnect/rotation counters)
- Configurable speech-focused MP3 output profile:
  - `AUDIO_TARGET_CHANNELS`
  - `AUDIO_TARGET_SAMPLE_RATE`
  - `AUDIO_MP3_VBR_QUALITY`
- Client negative-path coverage in tests:
  - Whisper HTTP error behavior
  - LLM HTTP/malformed response behavior
- Community/operations docs:
  - `CODE_OF_CONDUCT.md`
  - `PRODUCTION_CHECKLIST.md`

### Changed
- Migrated LLM integration naming to generic `LLM_*` configuration and `LLMClient` terminology.
- Updated `.env.example` and README to reflect generic OpenAI-compatible LLM endpoint usage.
- CI unit test step now includes client failure-path tests.

## [0.5.0] - 2026-02-20

### Added
- CI integration workflow with stub Whisper/LLM services (`tests/test_integration_pipeline.py`).
- Pre-commit quality gates with Ruff and repository hygiene hook.
- Mypy type-check stage in CI.
- Repository hygiene guard script to block accidental commit of `.env`, `data/`, session artifacts, and oversized files.
- Lightweight CLI to reprocess saved sessions from local audio artifacts:
  - `python -m chronicle_keeper.reprocess --session-dir ...`

### Changed
- Replaced runtime `print` calls with structured `logging` and log levels.
- Improved startup/retention/recovery observability with structured lifecycle logs and durations.
- Expanded README testing/quality instructions for Linux and Windows PowerShell.

## [0.4.0] - 2026-02-19

### Added
- P0 hardening:
  - persistent active runtime session state (`data/runtime/active_sessions.json`)
  - `ALLOW_PURGE_COMMANDS` safety switch for destructive purge commands
  - GitHub Actions secret scanning workflow (`gitleaks`)

## [0.3.0] - 2026-02-19

### Added
- Data lifecycle controls:
  - auto-cleanup settings (`AUTO_CLEANUP_ENABLED`, `AUTO_CLEANUP_ON_START`, `RETENTION_DAYS`)
  - manual cleanup/purge slash commands for server admins
- `RELEASE.md` with release hygiene checklist.
- Backup/restore operational guidance in README.

## [0.2.0] - 2026-02-19

### Added
- Docker Compose models integration for local LLM (`ai/gpt-oss:20B-MXFP4`).
- Whisper model configurability (`WHISPER_ASR_ENGINE`, `WHISPER_ASR_MODEL`, `WHISPER_ASR_MODEL_PATH`).
- Optional audio normalization (`AUDIO_NORMALIZE`).
- Long-session reliability features:
  - transcript chunking + hierarchical summarization
  - recording rotation (`RECORDING_ROTATION_SECONDS`)
  - processing checkpoints (`processing_state.json`)
  - startup recovery for unfinished sessions
- Artifact-first publishing to Discord:
  - transcript as attached `full_transcript.txt`
  - best-effort `.mp3` attachments
- Project governance docs:
  - `LICENSE`
  - `SECURITY.md`
  - `SUPPORT.md`

## [0.1.0] - 2026-02-19

### Added
- Initial public-ready version of Discord Chronicle Keeper:
  - Discord voice recording
  - Whisper transcription integration
  - local LLM summary generation
  - guild-level setup commands and language selection
