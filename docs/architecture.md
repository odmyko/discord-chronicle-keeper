# Architecture

## Overview

Discord Chronicle Keeper processes sessions with a local-first pipeline:

1. Bot records per-speaker audio from a Discord voice channel.
2. Audio is compressed to MP3 and stored in `data/sessions/<guild>/<session>/audio`.
3. Whisper ASR produces per-speaker transcripts and timeline segments.
4. Full transcript is assembled and summarized by a local OpenAI-compatible LLM.
5. Artifacts are published to the configured Discord text channel and persisted on disk.

## Runtime Components

- `bot` service:
  - Discord slash commands and voice recording control
  - orchestration for processing, retries, recovery, retention
- Whisper backend (choose one):
  - `whisper` (`/asr` style, onerahmet image)
  - `whisper_vllm` (`/v1/audio/transcriptions` style)
- LLM backend:
  - Docker Model Runner model (`ai/gpt-oss:20B-MXFP4`) or any compatible endpoint

## Processing Flow

1. `/chronicle_start` starts voice capture.
2. `/chronicle_stop` finalizes recording and triggers processing.
3. Session processor:
  - writes checkpoint state (`processing_state.json`)
  - transcribes tracks
  - builds `full_transcript.md` and `full_transcript.txt`
  - generates chunk summaries and final summary
4. Bot publishes summary and artifacts.

## Reliability Features

- segment rotation (`RECORDING_ROTATION_SECONDS`)
- startup recovery for unfinished sessions
- `reprocess` command from saved artifacts
- optional Whisper fallback endpoint
- config doctor warnings on startup

## Data Layout

- `data/guild_settings.json`: per-guild channel/language config
- `data/runtime/active_sessions.json`: active session runtime state
- `data/sessions/<guild>/<session>/`:
  - `audio/`
  - `transcripts/`
  - `summary.md`
  - `full_transcript.txt`
  - `processing_state.json`
