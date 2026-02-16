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
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`, then run:

```bash
python -m chronicle_keeper.bot
```

## Slash Commands

- `/chronicle_setup channel:#text-channel` - set where reports will be published.
- `/chronicle_start` - join your voice channel and start recording.
- `/chronicle_stop` - stop recording, build transcript and summary, publish to the chronicle channel.
- `/chronicle_leave` - disconnect the bot from voice.

## Current Limitations

- Transcription is generated per-user track. Very dense, second-by-second interleaving of speech between players is not reconstructed as a perfect chat log.
- Large sessions are better posted in parts: the bot already chunks long messages to fit Discord limits.
