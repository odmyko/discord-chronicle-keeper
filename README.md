# Discord Chronicle Keeper

Discord bot for DnD/TTTRPG with a fully local pipeline:
- record a Discord voice channel;
- transcribe audio through a local Whisper webservice (`onerahmet/openai-whisper-asr-webservice`);
- generate a summary and player-facing chronicle post through a local LLM in LM Studio;
- publish everything to a dedicated text channel for chronicles.

## Diarization Notes

Standalone diarization is not required: the bot receives separate tracks per Discord user (voice receive) and labels transcripts with Discord nicknames (`display_name`). In practice, this is usually better than post-diarization on a mixed track.

## Requirements

- Python 3.11+
- `ffmpeg` in `PATH` (for WAV -> MP3 compression)
- Discord bot token
- Local Whisper service (example: `http://127.0.0.1:9000`)
- LM Studio with OpenAI-compatible API enabled (usually `http://127.0.0.1:1234/v1`)

## Setup

```bash
# Linux / WSL
sudo apt update
sudo apt install -y python-is-python3 python3-venv
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

```powershell
# Windows PowerShell (recommended: Python 3.12)
cd E:\workspace\discord_chronicle_keeper
python -m venv .venv-win
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv-win\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pynacl
Copy-Item .env.example .env
```

```powershell
# Windows: install/verify ffmpeg for MP3 compression
winget install -e --id Gyan.FFmpeg
Get-Command ffmpeg
where.exe ffmpeg
ffmpeg -version
```

If `ffmpeg` is not found after install, restart PowerShell and check again.

Fill in `.env`, then run:

```bash
python -m chronicle_keeper.bot
```

Optional audio preprocessing:
- `AUDIO_NORMALIZE=false` (default): only MP3 compression.
- `AUDIO_NORMALIZE=true`: apply mild normalization (`highpass + loudnorm`) before Whisper.

Long session processing options:
- `PROCESSING_TIMEOUT_SECONDS=7200` sets max end-of-session processing time.
- `SUMMARY_CHUNK_CHARS=14000` controls transcript chunk size for hierarchical summarization.
- `RECORDING_ROTATION_SECONDS=1800` rotates recording into segments every 30 min (set `0` to disable).
- The bot now writes processing checkpoints to `data/sessions/<guild_id>/<session_ts>/processing_state.json`.
- For very long sessions, chunk summaries are saved in `summary_chunks/` and combined into final `summary.md`.
- In Discord, full transcript is posted as attached `full_transcript.txt` instead of inline long messages.
- Bot also attempts to upload recorded speaker `.mp3` files (Discord size limits may apply).

```powershell
python -m chronicle_keeper.bot
```

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
LMSTUDIO_BASE_URL=http://host.docker.internal:1234/v1
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
- The bot supports both generic `LLM_*` and `LMSTUDIO_*` env vars, so you can run LLM manually or through compose models.
- LLM model config sets max context `131072`.

### Local Whisper Model (CT2)

For custom local models with `faster_whisper`, convert and mount a CTranslate2 model:

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

## Slash Commands

- `/chronicle_setup` - set report text channel (dropdown channel picker).
- `/chronicle_setup_here` - set current text channel for reports.
- `/chronicle_setup_voice` - set default voice channel for recording (dropdown channel picker).
- `/chronicle_setup_voice_here` - set your current voice channel as default recording channel.
- `/chronicle_setup_channels` - one command to set both voice channel and transcript text channel.
- `/chronicle_setup_language` - set summary output language (`en`, `uk`, `ru`).
- `/chronicle_start` - start recording in configured default voice channel; if not configured, uses your current voice channel.
- `/chronicle_stop` - stop recording, build transcript and summary, publish to the chronicle channel.
- `/chronicle_leave` - disconnect the bot from voice.

## Current Limitations

- Transcription is generated per-user track. Very dense, second-by-second interleaving of speech between players is not reconstructed as a perfect chat log.
- Large sessions are better posted in parts: the bot already chunks long messages to fit Discord limits.
