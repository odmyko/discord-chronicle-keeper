# Discord Chronicle Keeper

Discord bot for DnD/TTRPG with a fully local pipeline:
- record a Discord voice channel;
- transcribe audio through a local Whisper webservice (`onerahmet/openai-whisper-asr-webservice`);
- generate a summary and player-facing chronicle post through a local LLM in LM Studio;
- publish everything to a dedicated text channel for chronicles.

## Quickstart (5 min)

1. Clone repo and install Python deps:
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```
Windows PowerShell equivalent:
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```
2. Fill `.env` (`DISCORD_BOT_TOKEN`, `WHISPER_BASE_URL`, `LLM_BASE_URL` or Compose model settings).
3. Start bot:
```bash
python -m chronicle_keeper.bot
```
4. In Discord, run:
- `/chronicle_setup_channels`
- `/chronicle_start`
- `/chronicle_stop`

## Architecture

```mermaid
flowchart LR
  A[Discord Voice Channel] --> B[Chronicle Keeper Bot]
  B --> C[Per-user Audio Segments mp3]
  C --> D[Whisper ASR]
  D --> E[Per-speaker Transcripts]
  E --> F[Chunked + Hierarchical Summarization]
  F --> G[Local LLM API]
  G --> H[Session Summary + Chronicle Post]
  E --> I[Session Artifacts on Disk]
  H --> J[Discord Text Chronicle Channel]
  I --> J
```

## Privacy and Consent

- This bot records voice conversations and stores local artifacts under `data/sessions/`.
- Use this bot only with explicit participant consent and in compliance with local laws/Discord policies.
- Review and manage retention of generated artifacts (`audio`, transcripts, summaries, checkpoints).
- Do not commit secrets or private session artifacts to git.
- See `SECURITY.md` for vulnerability reporting and `SUPPORT.md` for support channels.

## Diarization Notes

Standalone diarization is not required: the bot receives separate tracks per Discord user (voice receive) and labels transcripts with Discord nicknames (`display_name`). In practice, this is usually better than post-diarization on a mixed track.

## Requirements

- Python 3.11+
- `ffmpeg` in `PATH` (for WAV -> MP3 compression)
- Discord bot token
- Local Whisper service (example: `http://127.0.0.1:9000`)
- LM Studio with OpenAI-compatible API enabled (usually `http://127.0.0.1:1234/v1`)

## Detailed Setup

Quickstart above is enough for most users. Use this section for platform-specific prerequisites and tuning.

### Linux / WSL prerequisites
```bash
sudo apt update
sudo apt install -y python-is-python3 python3-venv
```

### Windows PowerShell prerequisites
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
# Optional fallback if voice deps fail:
pip install pynacl
```

### ffmpeg check (required for MP3 compression)
```powershell
winget install -e --id Gyan.FFmpeg
Get-Command ffmpeg
where.exe ffmpeg
ffmpeg -version
```

If `ffmpeg` is not found after install, restart PowerShell and check again.

### Runtime config highlights

- `AUDIO_NORMALIZE=false` (default): only MP3 compression.
- `AUDIO_NORMALIZE=true`: apply mild normalization (`highpass + loudnorm`) before Whisper.
- `AUDIO_MP3_VBR_QUALITY=4` (default): MP3 VBR quality (`0` best/largest .. `9` smallest).
- `AUDIO_TARGET_CHANNELS=0` / `AUDIO_TARGET_SAMPLE_RATE=0` (default): keep source channels/sample-rate.
- Speech-friendly preset example: `AUDIO_TARGET_CHANNELS=1`, `AUDIO_TARGET_SAMPLE_RATE=16000`, `AUDIO_MP3_VBR_QUALITY=5`.
- Extra compact preset example: `AUDIO_TARGET_CHANNELS=1`, `AUDIO_TARGET_SAMPLE_RATE=16000`, `AUDIO_MP3_VBR_QUALITY=6`.

Long session processing options:
- `PROCESSING_TIMEOUT_SECONDS=7200` sets max end-of-session processing time.
- `SUMMARY_CHUNK_CHARS=14000` controls transcript chunk size for hierarchical summarization.
- `RECORDING_ROTATION_SECONDS=1800` rotates recording into segments every 30 min (set `0` to disable).
- `RECOVERY_AUTO_POST_PARTIAL=true` attempts startup recovery post for unfinished sessions.
- `RECOVERY_MAX_SESSIONS=20` limits how many unfinished sessions are auto-posted per startup.
- Active runtime session state is persisted at `data/runtime/active_sessions.json`.
- The bot now writes processing checkpoints to `data/sessions/<guild_id>/<session_ts>/processing_state.json`.
- For very long sessions, chunk summaries are saved in `summary_chunks/` and combined into final `summary.md`.
- In Discord, full transcript is posted as attached `full_transcript.txt` instead of inline long messages.
- Bot also attempts to upload recorded speaker `.mp3` files (Discord size limits may apply).

## Docker

Build image:

```bash
docker build -t discord-chronicle-keeper .
```

Run container:

```bash
docker run --rm \
  --name discord-chronicle-keeper \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  discord-chronicle-keeper
```

For Docker on Windows/macOS, if Whisper and LM Studio run on your host machine,
set these in `.env`:

```env
WHISPER_BASE_URL=http://host.docker.internal:9000
LLM_BASE_URL=http://host.docker.internal:1234/v1
```

## Docker Compose (Bot + Whisper + LLM model)

This repo includes a full-stack compose setup:
- `bot`: Discord Chronicle Keeper
- `whisper`: `discord-chronicle-whisper5090:latest` (build recipe included)
- `llm` model via Docker Compose models: `ai/gpt-oss:20B-MXFP4`

Start all services:

```bash
docker compose up -d --build
```

If you previously used an `llm` service container, clean old compose objects first:

```bash
docker compose down --remove-orphans
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f bot
docker compose logs -f whisper
```

Stop:

```bash
docker compose down
```

Notes:
- Whisper Dockerfile is at `docker/whisper5090/Dockerfile` and reproduces the CUDA 12.8 torch patch for RTX 5090.
- Compose overrides Whisper URL to internal service name:
  - `WHISPER_BASE_URL=http://whisper:9000`
- Whisper model/engine are configurable via `.env`:
  - `WHISPER_ASR_ENGINE` (`openai_whisper` or `faster_whisper`)
  - `WHISPER_ASR_MODEL` (for example `large-v3-turbo`, `large-v3`, `distil-large-v3`)
  - `WHISPER_ASR_MODEL_PATH` (container path for cached/local models)
- Compose model injection sets:
  - `LLM_BASE_URL` (endpoint URL)
  - `LLM_MODEL` (selected model name)
- The bot uses generic `LLM_*` env vars, so you can run any OpenAI-compatible local endpoint manually or through compose models.
- LLM model config sets max context `131072`.

### Local Whisper Model (CT2)

For custom local models with `faster_whisper`, convert and mount a CTranslate2 model:

Install conversion dependencies in your active Python environment first:

```bash
python -m pip install ctranslate2 transformers torch
```

```bash
ct2-transformers-converter \
  --model anuragshas/whisper-large-v2-uk \
  --output_dir whisper-large-v2-uk \
  --quantization float16
```

Place converted files under `./data/whisper-models/whisper-large-v2-uk`, then set in `.env`:

```env
WHISPER_ASR_ENGINE=faster_whisper
WHISPER_ASR_MODEL=/models/whisper/whisper-large-v2-uk
WHISPER_ASR_MODEL_PATH=/models/whisper
```

Restart compose after changing model settings:

```bash
docker compose up -d --build
```

Helper script (converts + updates `.env` automatically):

```bash
python scripts/prepare_whisper_ct2_model.py \
  --model anuragshas/whisper-large-v2-uk \
  --quantization float16
```

Optional:
- `--output-name whisper-large-v2-uk`
- `--env-file .env`
- `--force` (overwrite existing converted directory)

If conversion previously succeeded but Whisper fails with mel-shape errors
(for example `expected ... 128 ... got ... 80 ...`), re-run conversion with:

```bash
python scripts/prepare_whisper_ct2_model.py --model <model-id> --quantization float16 --force
```

## Slash Commands

- `/chronicle_setup` - set report text channel (dropdown channel picker).
- `/chronicle_setup_here` - set current text channel for reports.
- `/chronicle_setup_voice` - set default voice channel for recording (dropdown channel picker).
- `/chronicle_setup_voice_here` - set your current voice channel as default recording channel.
- `/chronicle_setup_channels` - one command to set both voice channel and transcript text channel.
- `/chronicle_setup_language` - set summary output language (`en`, `uk`, `ru`).
- `/chronicle_status` - show current recorder status and reconnect/rotation counters.
- `/chronicle_reprocess_last` - reprocess latest saved session for this guild and republish transcript/summary.
- `/chronicle_start` - start recording in configured default voice channel; if not configured, uses your current voice channel.
- `/chronicle_stop` - stop recording, build transcript and summary, publish to the chronicle channel.
- `/chronicle_leave` - disconnect the bot from voice.
- `/chronicle_cleanup_now` - run retention cleanup immediately (Manage Server required).
- `/chronicle_purge_session` - delete one saved session by id (Manage Server required, `ALLOW_PURGE_COMMANDS=true`).
- `/chronicle_purge_guild_data` - delete all saved sessions for this guild (Manage Server required; requires `PURGE` confirmation and `ALLOW_PURGE_COMMANDS=true`).

## Current Limitations

- Transcription is generated per-user track. Very dense, second-by-second interleaving of speech between players is not reconstructed as a perfect chat log.
- Large sessions are better posted in parts: the bot already chunks long messages to fit Discord limits.
- Voice reconnect/recovery is best-effort; hard crashes can still lose in-memory data between segment rotations.
- Discord file size limits can prevent uploading all `.mp3` artifacts in-channel; full files remain on disk.
- Quality report is heuristic (duration/bitrate/reconnect/rotation counters) and not a full audio QA system.

## Versioning

This project follows Semantic Versioning (`MAJOR.MINOR.PATCH`):
- `PATCH`: bug fixes and internal improvements.
- `MINOR`: backward-compatible features.
- `MAJOR`: breaking changes.

Track releases and notable changes in `CHANGELOG.md`.
Release checklist is documented in `RELEASE.md`.

## License

This project is dual-licensed:
- `AGPL-3.0-or-later`
- Commercial license (for proprietary/commercial use without AGPL obligations)

See:
- `LICENSE`
- `COMMERCIAL_LICENSE.md`
- `CONTRIBUTING.md` (contribution licensing terms)

## Third-party Components

This repository references third-party Docker images, models, and dependencies.
They are licensed by their respective owners.
You are responsible for reviewing and complying with third-party license terms
when building, running, or redistributing derived artifacts.

## Data Lifecycle

Retention and cleanup are configurable in `.env`:
- `AUTO_CLEANUP_ENABLED=true|false` (default: `false`)
- `AUTO_CLEANUP_ON_START=true|false` (default: `false`)
- `RETENTION_DAYS=<N>`
- `ALLOW_PURGE_COMMANDS=true|false` (default: `false`)

Manual lifecycle commands:
- `/chronicle_cleanup_now`
- `/chronicle_purge_session`
- `/chronicle_purge_guild_data`

## Backup and Restore

Recommended backup target:
- `data/guild_settings.json`
- `data/sessions/`

Example backup:

```bash
tar -czf chronicle-backup-$(date +%Y%m%d_%H%M%S).tar.gz data/guild_settings.json data/sessions
```

Example restore:

```bash
tar -xzf chronicle-backup-YYYYMMDD_HHMMSS.tar.gz
```

After restore:
1. Start bot.
2. Review startup recovery messages in chronicle channels.
3. Run `/chronicle_cleanup_now` if retention policy should be applied immediately.

## Reprocess Saved Session (CLI)

Use this to rebuild transcript/summary from already recorded audio artifacts:

```bash
python -m chronicle_keeper.reprocess --session-dir data/sessions/<guild_id>/<session_id> --language ru
```

Alternative form:

```bash
python -m chronicle_keeper.reprocess --guild-id <guild_id> --session-id <session_id> --language ru
```

This command reads files from `audio/`, re-runs Whisper + LLM processing, and rewrites:
- `transcripts/*.md`
- `full_transcript.md`
- `full_transcript.txt`
- `summary.md`
- `summary_chunks/*.md` (for chunked runs)

## Testing

Install dev dependencies:

```bash
pip install -r requirements-dev.txt
```

Install pre-commit hooks:

```bash
pre-commit install
```

Run checks from repository root:

Linux / WSL:
```bash
pre-commit run --all-files
python scripts/check_repo_hygiene.py
python -m compileall chronicle_keeper
python -m mypy
python -m pytest -q
```

Windows PowerShell:
```powershell
pre-commit run --all-files
python scripts/check_repo_hygiene.py
python -m compileall chronicle_keeper
python -m mypy
python -m pytest -q
```

## Security

Security policy and vulnerability reporting: `SECURITY.md`

## Support

Support options and channels: `SUPPORT.md`

## Community

- Code of Conduct: `CODE_OF_CONDUCT.md`
- Production readiness checklist: `PRODUCTION_CHECKLIST.md`

