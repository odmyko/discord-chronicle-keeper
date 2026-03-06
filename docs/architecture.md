# Architecture

## Overview

Discord Chronicle Keeper is a local-first pipeline:

1. Bot records per-speaker audio from Discord voice receive.
2. Audio chunks are stored in `data/sessions/<guild>/<session>/audio`.
3. Qwen3-ASR (local Python inference) generates transcript segments.
4. Session processor builds transcript artifacts.
5. Local OpenAI-compatible LLM generates summary and chronicle post.
6. Bot posts results to Discord text channel and keeps artifacts on disk.

## Runtime Components

- `bot` (default compose service):
  - Discord slash commands
  - voice recording + reconnect/rotation logic
  - transcription + summarization orchestration
- `bot_docker_llm` (compose profile `docker-llm`):
  - same bot code, but LLM endpoint comes from Docker model runner
- ASR runtime:
  - local Qwen3-ASR via Python (`QWEN3_ASR_*` env)

## Processing Flow

1. `/chronicle_start` starts recording for active campaign.
2. Bot writes speaker chunks to disk and rotates by `RECORDING_ROTATION_SECONDS`.
3. `/chronicle_stop` finalizes current session and starts processing.
4. Processor:
   - updates `processing_state.json`
   - transcribes audio
   - builds `full_transcript.md` and `full_transcript.txt`
   - generates chunk summaries and final `summary.md`
5. Bot posts summary and available artifacts to chronicle channel.

## Reliability Features

- voice reconnect monitor + manual `/chronicle_reconnect`
- decode-burst guard with forced rollover/reconnect
- recording rotation with resume attempt
- startup recovery for unfinished sessions
- reprocess commands from saved artifacts
- startup config doctor warnings for obvious misconfigurations

## Data Layout

- `data/guild_settings.json`: guild/campaign settings
- `data/runtime/active_sessions.json`: live runtime session registry
- `data/sessions/<guild>/<session>/`:
  - `audio/`
  - `transcripts/`
  - `summary.md`
  - `full_transcript.txt`
  - `processing_state.json`
