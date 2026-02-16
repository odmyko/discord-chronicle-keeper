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
py -3.12 -m venv .venv-win
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv-win\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pynacl
Copy-Item .env.example .env
```

Fill in `.env`, then run:

```bash
python -m chronicle_keeper.bot
```

```powershell
python -m chronicle_keeper.bot
```

## Slash Commands

- `/chronicle_setup` - set report text channel (dropdown channel picker).
- `/chronicle_setup_here` - set current text channel for reports.
- `/chronicle_setup_voice` - set default voice channel for recording (dropdown channel picker).
- `/chronicle_setup_voice_here` - set your current voice channel as default recording channel.
- `/chronicle_setup_channels` - one command to set both voice channel and transcript text channel.
- `/chronicle_start` - start recording in configured default voice channel; if not configured, uses your current voice channel.
- `/chronicle_stop` - stop recording, build transcript and summary, publish to the chronicle channel.
- `/chronicle_leave` - disconnect the bot from voice.

## Current Limitations

- Transcription is generated per-user track. Very dense, second-by-second interleaving of speech between players is not reconstructed as a perfect chat log.
- Large sessions are better posted in parts: the bot already chunks long messages to fit Discord limits.
