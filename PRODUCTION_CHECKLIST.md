# Production Checklist

Use this checklist before running Chronicle Keeper for real sessions.

## Discord Setup

- Bot is invited with required permissions:
  - View Channel
  - Connect
  - Speak (recommended)
  - Send Messages
  - Attach Files
- Slash commands are visible and executable for target channels/roles.
- Bot can access both voice channel and chronicle text channel.

## Runtime Configuration

- `.env` has valid values:
  - `DISCORD_BOT_TOKEN`
  - `WHISPER_BASE_URL`
  - `LLM_BASE_URL`
  - `LLM_MODEL`
- Audio profile tuned for speech (if desired):
  - `AUDIO_TARGET_CHANNELS=1`
  - `AUDIO_TARGET_SAMPLE_RATE=16000`
  - `AUDIO_MP3_VBR_QUALITY=5`
- Rotation/timeout tuned for expected session length:
  - `RECORDING_ROTATION_SECONDS`
  - `PROCESSING_TIMEOUT_SECONDS`

## Infrastructure

- `ffmpeg`/`ffprobe` are available in `PATH`.
- Whisper and LLM endpoints are reachable from the bot container/host.
- Firewall/network policy allows outbound Discord voice/websocket traffic.

## Data Safety

- Retention policy decided and configured:
  - `AUTO_CLEANUP_ENABLED`
  - `AUTO_CLEANUP_ON_START`
  - `RETENTION_DAYS`
- Backup procedure tested for:
  - `data/guild_settings.json`
  - `data/sessions/`
- Purge commands are disabled unless explicitly needed:
  - `ALLOW_PURGE_COMMANDS=false`

## Validation Run

- Run one short dry-run session:
  1. `/chronicle_setup_channels`
  2. `/chronicle_start`
  3. speak for 1-2 minutes
  4. `/chronicle_stop`
- Confirm:
  - transcript artifact is posted,
  - summary is generated,
  - `/chronicle_status` counters look healthy.

## CI/Repo Hygiene

- `python -m pytest -q` passes.
- `pre-commit run --all-files` passes.
- `python scripts/check_repo_hygiene.py` passes.
- No secrets are committed.
