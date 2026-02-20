# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project aims to follow Semantic Versioning.

## [Unreleased]

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
