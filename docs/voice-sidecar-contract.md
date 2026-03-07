# Voice Sidecar Contract (Draft, Phase 1)

This document defines the integration contract between Python bot and Node voice sidecar.

Current status:
- Sidecar now has a **minimal live voice receive backend** (`discordjs-voice`).
- It joins voice channel, records per-user WAV tracks, rotates segments, and stops cleanly.
- Contract remains draft and can still evolve.

## Runtime model

- Service name: `voice_sidecar`
- Base URL: `http://voice_sidecar:8081` (compose network) or `http://127.0.0.1:8081` (local)
- Required env for live recording:
  - `DISCORD_BOT_TOKEN`
- Optional auth header:
  - `X-Sidecar-Token: <SIDECAR_TOKEN>`

## Endpoints

### `GET /health`

Response:

```json
{
  "ok": true,
  "service": "chronicle-voice-sidecar",
  "mode": "skeleton",
  "sessions_running": 1
}
```

### `GET /v1/status`

Response: all tracked sessions in memory/state file.

### `GET /v1/sessions/{guild_id}/status`

- `404` if no active/stored session for guild.

### `POST /v1/sessions/start`

Request:

```json
{
  "guild_id": 1472990314284187938,
  "voice_channel_id": 1472990315047813196,
  "text_channel_id": 1472992722192302214,
  "requested_by": 451102877562306570,
  "campaign_id": "df1ce0f33a",
  "campaign_name": "Legacy of Davos",
  "summary_language": "ru",
  "session_context": "",
  "name_hints": ""
}
```

Behavior:
- Starts new recording session for guild.
- Idempotent if same guild/channel already recording.
- `409` if another session already recording in different voice channel.
- Creates/uses session folder:
  - `data/sessions/<guild_id>/<session_id>/audio`
- Writes WAV tracks named:
  - `<speaker>_<user_id>_segNNN.wav`

### `POST /v1/sessions/rotate`

Request:

```json
{
  "guild_id": 1472990314284187938,
  "reason": "timer"
}
```

Behavior:
- Increments `segments_written` and updates `last_rotation_*`.
- `404` if no session.
- `409` if session is not recording.
- Closes current segment files and starts next segment.

### `POST /v1/sessions/stop`

Request:

```json
{
  "guild_id": 1472990314284187938,
  "reason": "manual"
}
```

Behavior:
- Marks session `stopped`.
- Idempotent if already stopped.
- Finalizes active streams and closes voice connection.

## Session state shape

Sidecar tracks one session per guild:

```json
{
  "guild_id": 1472990314284187938,
  "voice_channel_id": 1472990315047813196,
  "session_id": "20260307_112233",
  "started_at_utc": "2026-03-07T11:22:33.123Z",
  "status": "recording",
  "segments_written": 0,
  "backend": "discordjs-voice",
  "session_dir": "/app/data/sessions/1472990314284187938/20260307_113355"
}
```

Persisted file:
- `data/runtime/voice_sidecar_state.json`

## Integration rules for Python bot (next phase)

1. Use sidecar API for `/chronicle_start`, `/chronicle_stop`, rotation orchestration.
2. Keep existing ASR + summary pipeline unchanged.
3. Session folder naming and audio segment naming must remain compatible with current processor parser:
   - `<speaker>_<user_id>_segNNN.(mp3|wav|...)`.
4. On `stop`, Python must wait until sidecar confirms session status `stopped` before running reprocess.
