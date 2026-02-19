# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog and this project aims to follow Semantic Versioning.

## [Unreleased]

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
