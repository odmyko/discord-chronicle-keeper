import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
import json
import logging
from pathlib import Path
import re
import shutil
import struct
import time
from typing import Any, Awaitable, Callable, Iterable

import discord
from discord.ext import commands
from discord.sinks.errors import RecordingException
import discord.gateway as discord_gateway

from .config import Settings, config_doctor_issues, load_settings
from .llm_client import LLMClient
from .metrics import RuntimeMetrics
from .processor import SessionProcessor
from .storage import GuildSettingsStore
from .whisper_client import WhisperClient


DISCORD_SAFE_LIMIT = 1900
VoiceLikeChannel = discord.VoiceChannel
DoneCallback = Callable[
    [discord.sinks.Sink, discord.abc.Messageable, int], Awaitable[None]
]
logger = logging.getLogger(__name__)


def chunk_text(text: str, limit: int = DISCORD_SAFE_LIMIT) -> Iterable[str]:
    if len(text) <= limit:
        yield text
        return

    chunk: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and chunk:
            yield "".join(chunk)
            chunk = [line]
            size = len(line)
        else:
            chunk.append(line)
            size += len(line)
    if chunk:
        yield "".join(chunk)


@dataclass
class GuildRecordingState:
    sink: discord.sinks.Sink | None = None
    processing: bool = False
    voice_channel_id: int | None = None
    health_task: asyncio.Task | None = None
    rotation_task: asyncio.Task | None = None
    finalizing: bool = False
    segment_sinks: list[discord.sinks.Sink] | None = None
    started_at_utc: datetime | None = None
    rotation_triggered: int = 0
    rotation_resumed: int = 0
    rotation_failed: int = 0
    reconnect_attempts: int = 0
    reconnect_successes: int = 0
    reconnect_failures: int = 0
    decode_burst_triggers: int = 0
    decode_burst_skipped_cooldown: int = 0
    last_decode_burst_at_utc: str | None = None
    decode_recovery_cooldown_until: float = 0.0
    campaign_id: str = ""
    campaign_name: str = ""
    summary_language: str = "ru"
    session_context: str = ""
    name_hints: str = ""
    done_callback: DoneCallback | None = None
    fallback_channel: discord.abc.Messageable | None = None
    session_id: str = ""
    session_dir: Path | None = None
    persisted_segments: int = 0
    restart_in_progress: bool = False


def build_bot(settings: Settings) -> commands.Bot:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # Compatibility patch for py-cord voice mode negotiation.
    # Some Discord regions may advertise only newer AEAD mode names, and older
    # supported_modes lists can cause gateway.py to fail with IndexError.
    extra_voice_modes = (
        "aead_aes256_gcm_rtpsize",
        "aead_xchacha20_poly1305_rtpsize",
        "xsalsa20_poly1305_lite_rtpsize",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305_lite",
    )
    try:
        supported_modes = list(
            getattr(discord.VoiceClient, "supported_modes", ()) or ()
        )
        for mode in extra_voice_modes:
            if mode not in supported_modes:
                supported_modes.append(mode)
        discord.VoiceClient.supported_modes = tuple(supported_modes)
    except Exception:
        pass

    # Prefer xchacha mode when available. Some py-cord builds can connect with AES mode
    # but produce decode errors on receive in certain environments.
    try:
        if not getattr(
            discord_gateway.DiscordVoiceWebSocket, "_chronicle_mode_patch", False
        ):

            async def _patched_initial_connection(self, data):
                state = self._connection
                state.ssrc = data["ssrc"]
                state.voice_port = data["port"]
                state.endpoint_ip = data["ip"]

                packet = bytearray(74)
                struct.pack_into(">H", packet, 0, 1)
                struct.pack_into(">H", packet, 2, 70)
                struct.pack_into(">I", packet, 4, state.ssrc)
                state.socket.sendto(packet, (state.endpoint_ip, state.voice_port))
                recv = await self.loop.sock_recv(state.socket, 74)

                ip_start = 8
                ip_end = recv.index(0, ip_start)
                state.ip = recv[ip_start:ip_end].decode("ascii")
                state.port = struct.unpack_from(">H", recv, len(recv) - 2)[0]

                modes = [
                    mode
                    for mode in data["modes"]
                    if mode in self._connection.supported_modes
                ]
                preferred_order = (
                    "aead_xchacha20_poly1305_rtpsize",
                    "aead_aes256_gcm_rtpsize",
                    "xsalsa20_poly1305_lite",
                    "xsalsa20_poly1305_suffix",
                    "xsalsa20_poly1305",
                )
                mode = None
                for preferred in preferred_order:
                    if preferred in modes:
                        mode = preferred
                        break
                if mode is None:
                    mode = modes[0]

                await self.select_protocol(state.ip, state.port, mode)
                discord_gateway._log.info(
                    "selected the voice protocol for use (%s)", mode
                )

            discord_gateway.DiscordVoiceWebSocket.initial_connection = (
                _patched_initial_connection
            )
            discord_gateway.DiscordVoiceWebSocket._chronicle_mode_patch = True
    except Exception:
        pass

    # Runtime support for Discord's AES-GCM RTP size mode when py-cord lacks methods.
    try:
        import nacl.bindings

        if not hasattr(discord.VoiceClient, "_encrypt_aead_aes256_gcm_rtpsize"):

            def _encrypt_aead_aes256_gcm_rtpsize(self, header: bytes, data) -> bytes:
                nonce = bytearray(12)
                nonce[:4] = struct.pack(">I", self._lite_nonce)
                self.checked_add("_lite_nonce", 1, 4294967295)
                ciphertext = nacl.bindings.crypto_aead_aes256gcm_encrypt(
                    bytes(data),
                    bytes(header),
                    bytes(nonce),
                    bytes(self.secret_key),
                )
                return header + ciphertext + nonce[:4]

            setattr(
                discord.VoiceClient,
                "_encrypt_aead_aes256_gcm_rtpsize",
                _encrypt_aead_aes256_gcm_rtpsize,
            )

        if not hasattr(discord.VoiceClient, "_decrypt_aead_aes256_gcm_rtpsize"):

            def _decrypt_aead_aes256_gcm_rtpsize(self, header, data):
                nonce = bytearray(12)
                nonce[:4] = data[-4:]
                payload = data[:-4]
                decrypted = nacl.bindings.crypto_aead_aes256gcm_decrypt(
                    bytes(payload),
                    bytes(header),
                    bytes(nonce),
                    bytes(self.secret_key),
                )
                # Discord prepends 8 bytes before opus payload for *_rtpsize modes.
                return decrypted[8:]

            setattr(
                discord.VoiceClient,
                "_decrypt_aead_aes256_gcm_rtpsize",
                _decrypt_aead_aes256_gcm_rtpsize,
            )
    except Exception:
        pass

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.guilds = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    store = GuildSettingsStore(settings.data_dir / "guild_settings.json")
    whisper = WhisperClient(settings)
    llm = LLMClient(settings)
    metrics = RuntimeMetrics()
    processor = SessionProcessor(
        settings.data_dir,
        whisper,
        llm,
        audio_dual_pipeline_enabled=settings.audio_dual_pipeline_enabled,
        audio_normalize=settings.audio_normalize,
        audio_vad_enabled=settings.audio_vad_enabled,
        audio_target_sample_rate=settings.audio_target_sample_rate,
        audio_target_channels=settings.audio_target_channels,
        audio_mp3_vbr_quality=settings.audio_mp3_vbr_quality,
        summary_chunk_chars=settings.summary_chunk_chars,
        metrics=metrics,
    )
    guild_state: dict[int, GuildRecordingState] = {}
    decode_error_events: deque[float] = deque()

    class OpusDecodeErrorHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage().lower()
            # py-cord usually logs two lines per decode issue; count one signal marker.
            if "opus_decode" not in message:
                return
            now = time.monotonic()
            decode_error_events.append(now)
            window = max(1, settings.voice_decode_burst_window_seconds)
            while decode_error_events and (now - decode_error_events[0]) > window:
                decode_error_events.popleft()

    opus_logger = logging.getLogger("discord.opus")
    if not getattr(opus_logger, "_chronicle_decode_handler", False):
        opus_logger.addHandler(OpusDecodeErrorHandler())
        opus_logger._chronicle_decode_handler = True

    async def send_long(channel: discord.abc.Messageable, text: str) -> None:
        for chunk in chunk_text(text):
            await channel.send(chunk)

    async def try_send(channel: discord.abc.Messageable | None, text: str) -> bool:
        started = time.perf_counter()
        if channel is None:
            metrics.observe("discord_publish", time.perf_counter() - started, False)
            return False
        try:
            await channel.send(text)
            metrics.observe("discord_publish", time.perf_counter() - started, True)
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            metrics.observe("discord_publish", time.perf_counter() - started, False)
            return False

    async def try_send_file(
        channel: discord.abc.Messageable | None,
        path: str,
        content: str | None = None,
    ) -> bool:
        started = time.perf_counter()
        if channel is None:
            metrics.observe("discord_publish", time.perf_counter() - started, False)
            return False
        try:
            await channel.send(content=content, file=discord.File(path))
            metrics.observe("discord_publish", time.perf_counter() - started, True)
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            metrics.observe("discord_publish", time.perf_counter() - started, False)
            return False

    async def try_send_files(
        channel: discord.abc.Messageable | None,
        paths: list[str],
        content: str | None = None,
        batch_size: int = 5,
    ) -> int:
        started = time.perf_counter()
        if channel is None or not paths:
            metrics.observe("discord_publish", time.perf_counter() - started, False)
            return 0
        sent = 0
        for i in range(0, len(paths), batch_size):
            files = [discord.File(p) for p in paths[i : i + batch_size]]
            try:
                await channel.send(content=content if i == 0 else None, files=files)
                sent += len(files)
                metrics.observe("discord_publish", 0.0, True)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                metrics.observe("discord_publish", 0.0, False)
                continue
        metrics.observe("discord_publish", time.perf_counter() - started, sent > 0)
        return sent

    def same_messageable(
        a: discord.abc.Messageable | None, b: discord.abc.Messageable | None
    ) -> bool:
        if a is None or b is None:
            return False
        a_id = getattr(a, "id", None)
        b_id = getattr(b, "id", None)
        return a_id is not None and b_id is not None and a_id == b_id

    async def ffprobe_audio(
        path: str,
    ) -> tuple[float, int | None, int | None, int | None]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,bit_rate",
                "-show_entries",
                "stream=sample_rate,channels",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return 0.0, None, None, None
        out, _ = await proc.communicate()
        if proc.returncode != 0 or not out:
            return 0.0, None, None, None
        lines = [
            line.strip()
            for line in out.decode("utf-8", errors="ignore").splitlines()
            if line.strip()
        ]
        # ffprobe default writer prints format entries first, then stream entries.
        # Expected order for our query: duration, bit_rate, sample_rate, channels.
        duration = 0.0
        bit_rate: int | None = None
        sample_rate: int | None = None
        channels: int | None = None
        if len(lines) > 0:
            try:
                duration = float(lines[0])
            except ValueError:
                duration = 0.0
        if len(lines) > 1:
            try:
                bit_rate = int(float(lines[1]))
            except ValueError:
                bit_rate = None
        if len(lines) > 2:
            try:
                sample_rate = int(lines[2])
            except ValueError:
                sample_rate = None
        if len(lines) > 3:
            try:
                channels = int(lines[3])
            except ValueError:
                channels = None
        return duration, bit_rate, sample_rate, channels

    async def build_quality_report(
        state: GuildRecordingState,
        speaker_items: list,
    ) -> str:
        durations: list[float] = []
        bitrates: list[int] = []
        sample_rates: list[int] = []
        channels_list: list[int] = []
        for item in speaker_items:
            duration, bit_rate, sample_rate, channels = await ffprobe_audio(
                str(item.audio_path)
            )
            if duration > 0:
                durations.append(duration)
            if bit_rate:
                bitrates.append(bit_rate)
            if sample_rate:
                sample_rates.append(sample_rate)
            if channels:
                channels_list.append(channels)

        total_duration = sum(durations)
        avg_bitrate_kbps = (
            (sum(bitrates) / len(bitrates) / 1000.0) if bitrates else None
        )
        dominant_sample_rate = (
            max(sample_rates, key=sample_rates.count) if sample_rates else None
        )
        dominant_channels = (
            max(channels_list, key=channels_list.count) if channels_list else None
        )
        elapsed_s = (
            (datetime.now(UTC) - state.started_at_utc).total_seconds()
            if state.started_at_utc is not None
            else None
        )

        lines = ["## Recording Quality Report"]
        lines.append(f"- Speaker tracks: `{len(speaker_items)}`")
        lines.append(
            f"- Rotation events: `{state.rotation_triggered}` (resumed `{state.rotation_resumed}`, failed `{state.rotation_failed}`)"
        )
        lines.append(
            f"- Reconnect attempts: `{state.reconnect_attempts}` (success `{state.reconnect_successes}`, failed `{state.reconnect_failures}`)"
        )
        if elapsed_s is not None:
            lines.append(f"- Session wall time: `{elapsed_s:.1f}s`")
        if total_duration > 0:
            lines.append(f"- Sum of track durations: `{total_duration:.1f}s`")
        if avg_bitrate_kbps is not None:
            lines.append(f"- Average encoded bitrate: `{avg_bitrate_kbps:.1f} kbps`")
        if dominant_sample_rate is not None:
            lines.append(f"- Sample rate (dominant): `{dominant_sample_rate} Hz`")
        if dominant_channels is not None:
            lines.append(f"- Channels (dominant): `{dominant_channels}`")
        lines.append(
            "- Tip: if audio sounds choppy, try `RECORDING_ROTATION_SECONDS=0` and monitor reconnect counters."
        )
        return "\n".join(lines)

    def stop_background_tasks(state: GuildRecordingState) -> None:
        if state.health_task is not None:
            state.health_task.cancel()
            state.health_task = None
        if state.rotation_task is not None:
            state.rotation_task.cancel()
            state.rotation_task = None

    def load_json_file(path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def save_json_file(path: str, payload: dict) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def runtime_state_path() -> str:
        runtime_dir = settings.data_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return str(runtime_dir / "active_sessions.json")

    def load_runtime_state() -> dict:
        payload = load_json_file(runtime_state_path())
        if not isinstance(payload, dict):
            return {"active_sessions": {}}
        if "active_sessions" not in payload or not isinstance(
            payload.get("active_sessions"), dict
        ):
            payload["active_sessions"] = {}
        return payload

    def save_runtime_state(payload: dict) -> None:
        save_json_file(runtime_state_path(), payload)

    def upsert_active_session(
        guild_id: int,
        *,
        status: str,
        voice_channel_id: int | None = None,
        chronicle_channel_id: int | None = None,
        segment_count: int | None = None,
        finalizing: bool | None = None,
    ) -> None:
        payload = load_runtime_state()
        key = str(guild_id)
        current = payload["active_sessions"].get(key, {})
        now = datetime.now(UTC).isoformat()
        entry = {
            "guild_id": guild_id,
            "status": status,
            "voice_channel_id": voice_channel_id
            if voice_channel_id is not None
            else current.get("voice_channel_id"),
            "chronicle_channel_id": (
                chronicle_channel_id
                if chronicle_channel_id is not None
                else current.get("chronicle_channel_id")
            ),
            "segment_count": segment_count
            if segment_count is not None
            else current.get("segment_count", 0),
            "finalizing": finalizing
            if finalizing is not None
            else current.get("finalizing", False),
            "rotation_seconds": settings.recording_rotation_seconds,
            "started_at_utc": current.get("started_at_utc", now),
            "updated_at_utc": now,
        }
        payload["active_sessions"][key] = entry
        save_runtime_state(payload)

    def clear_active_session(guild_id: int) -> None:
        payload = load_runtime_state()
        payload["active_sessions"].pop(str(guild_id), None)
        save_runtime_state(payload)

    def prune_stale_active_sessions(max_age_seconds: int = 300) -> int:
        payload = load_runtime_state()
        active = payload.get("active_sessions", {})
        if not isinstance(active, dict):
            return 0
        now = datetime.now(UTC)
        removed = 0
        for guild_key in list(active.keys()):
            entry = active.get(guild_key)
            if not isinstance(entry, dict):
                active.pop(guild_key, None)
                removed += 1
                continue
            updated_raw = str(entry.get("updated_at_utc") or "").strip()
            if not updated_raw:
                active.pop(guild_key, None)
                removed += 1
                continue
            try:
                updated_dt = datetime.fromisoformat(updated_raw)
                if updated_dt.tzinfo is None:
                    updated_dt = updated_dt.replace(tzinfo=UTC)
            except ValueError:
                active.pop(guild_key, None)
                removed += 1
                continue
            age = (now - updated_dt).total_seconds()
            if age <= max(30, max_age_seconds):
                continue
            active.pop(guild_key, None)
            removed += 1
        if removed > 0:
            payload["active_sessions"] = active
            save_runtime_state(payload)
        return removed

    def ensure_session_dir(state: GuildRecordingState, guild_id: int) -> Path:
        if state.session_id.strip():
            session_id = state.session_id.strip()
        else:
            session_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            state.session_id = session_id
        if state.session_dir is None:
            state.session_dir = (
                settings.data_dir / "sessions" / str(guild_id) / session_id
            )
        state.session_dir.mkdir(parents=True, exist_ok=True)
        (state.session_dir / "audio").mkdir(parents=True, exist_ok=True)
        return state.session_dir

    def write_session_checkpoint(
        state: GuildRecordingState,
        guild_id: int,
        *,
        status: str,
        total_tracks: int | None = None,
    ) -> None:
        session_dir = ensure_session_dir(state, guild_id)
        checkpoint_path = session_dir / "processing_state.json"
        checkpoint = load_json_file(str(checkpoint_path)) or {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        checkpoint.update(
            {
                "guild_id": guild_id,
                "started_at_utc": state.started_at_utc.isoformat()
                if state.started_at_utc
                else checkpoint.get("started_at_utc", datetime.now(UTC).isoformat()),
                "campaign_id": state.campaign_id,
                "campaign_name": state.campaign_name,
                "summary_language_used": state.summary_language,
                "session_context_used": state.session_context,
                "name_hints_used": state.name_hints,
                "status": status,
                "segments_total": max(0, state.persisted_segments),
                "persisted_segments": max(0, state.persisted_segments),
                "total_tracks": total_tracks
                if total_tracks is not None
                else checkpoint.get("total_tracks", 0),
            }
        )
        save_json_file(str(checkpoint_path), checkpoint)

    async def persist_recording_segment(
        guild: discord.Guild,
        state: GuildRecordingState,
        sink: discord.sinks.Sink | None,
        *,
        fallback_channel: discord.abc.Messageable | None = None,
    ) -> bool:
        if sink is None or not getattr(sink, "audio_data", None) or not sink.audio_data:
            return False
        try:
            ensure_session_dir(state, guild.id)
            segment_index = state.persisted_segments + 1
            written = await processor.save_recording_segment(
                guild=guild,
                sink=sink,
                session_dir=state.session_dir or ensure_session_dir(state, guild.id),
                segment_index=segment_index,
            )
            if not written:
                return False
            state.persisted_segments += 1
            write_session_checkpoint(
                state,
                guild.id,
                status="recording" if not state.finalizing else "processing",
                total_tracks=len(sink.audio_data),
            )
            return True
        except Exception:
            logger.exception(
                "[segment] persist_failed guild_id=%s segment=%s",
                guild.id,
                state.persisted_segments + 1,
            )
            if fallback_channel is not None:
                await try_send(
                    fallback_channel,
                    "Warning: failed to persist current recording segment to disk.",
                )
            return False

    def session_timestamp_utc(session_name: str) -> datetime | None:
        try:
            return datetime.strptime(session_name, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return None

    def latest_session_dir_for_guild(guild_id: int) -> Path | None:
        guild_sessions_dir = settings.data_dir / "sessions" / str(guild_id)
        if not guild_sessions_dir.exists() or not guild_sessions_dir.is_dir():
            return None
        candidates = [
            p
            for p in guild_sessions_dir.iterdir()
            if p.is_dir() and session_timestamp_utc(p.name) is not None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.name, reverse=True)
        return candidates[0]

    def list_session_dirs_for_guild(guild_id: int, limit: int = 20) -> list[Path]:
        guild_sessions_dir = settings.data_dir / "sessions" / str(guild_id)
        if not guild_sessions_dir.exists() or not guild_sessions_dir.is_dir():
            return []
        candidates = [
            p
            for p in guild_sessions_dir.iterdir()
            if p.is_dir() and session_timestamp_utc(p.name) is not None
        ]
        candidates.sort(key=lambda p: p.name, reverse=True)
        return candidates[: max(1, limit)]

    def read_session_snapshot(session_dir: Path) -> dict[str, str]:
        checkpoint = load_json_file(str(session_dir / "processing_state.json")) or {}
        if not isinstance(checkpoint, dict):
            checkpoint = {}
        return {
            "campaign_id": str(checkpoint.get("campaign_id") or "").strip(),
            "campaign_name": str(checkpoint.get("campaign_name") or "").strip(),
            "summary_language": str(checkpoint.get("summary_language_used") or "ru")
            .strip()
            .lower()
            or "ru",
            "session_context": str(
                checkpoint.get("session_context_used") or ""
            ).strip(),
            "name_hints": str(checkpoint.get("name_hints_used") or "").strip(),
        }

    def list_session_dirs_for_campaign(
        guild_id: int,
        campaign_id: str,
        *,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 200,
    ) -> list[Path]:
        session_dirs = list_session_dirs_for_guild(guild_id, limit=5000)
        selected: list[Path] = []
        for session_dir in session_dirs:
            ts = session_timestamp_utc(session_dir.name)
            if ts is None:
                continue
            if from_date and ts.date() < from_date.date():
                continue
            if to_date and ts.date() > to_date.date():
                continue
            snapshot = read_session_snapshot(session_dir)
            if snapshot.get("campaign_id") != campaign_id:
                continue
            selected.append(session_dir)
            if len(selected) >= max(1, limit):
                break
        return selected

    def parse_yyyy_mm_dd(value: str) -> datetime | None:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            return None

    async def campaign_autocomplete(
        ctx: discord.AutocompleteContext,
    ) -> list[discord.OptionChoice]:
        interaction = getattr(ctx, "interaction", None)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            return []
        query = str(getattr(ctx, "value", "") or "").strip().lower()
        choices: list[discord.OptionChoice] = []
        for campaign in store.list_campaigns(guild.id):
            campaign_id = str(campaign.get("id") or "").strip()
            campaign_name = str(campaign.get("name") or "").strip()
            if not campaign_id:
                continue
            if (
                query
                and query not in campaign_id.lower()
                and query not in campaign_name.lower()
            ):
                continue
            label = f"{campaign_name} ({campaign_id})" if campaign_name else campaign_id
            choices.append(discord.OptionChoice(name=label[:100], value=campaign_id))
            if len(choices) >= 25:
                break
        return choices

    async def session_autocomplete(
        ctx: discord.AutocompleteContext,
    ) -> list[discord.OptionChoice]:
        interaction = getattr(ctx, "interaction", None)
        guild = getattr(interaction, "guild", None)
        if guild is None:
            return []
        query = str(getattr(ctx, "value", "") or "").strip().lower()
        choices: list[discord.OptionChoice] = []
        for session_dir in list_session_dirs_for_guild(guild.id, limit=50):
            session_id = session_dir.name
            snapshot = read_session_snapshot(session_dir)
            campaign_name = str(snapshot.get("campaign_name") or "").strip()
            if (
                query
                and query not in session_id.lower()
                and query not in campaign_name.lower()
            ):
                continue
            label = f"{session_id} | {campaign_name}" if campaign_name else session_id
            choices.append(discord.OptionChoice(name=label[:100], value=session_id))
            if len(choices) >= 25:
                break
        return choices

    def cleanup_old_sessions(retention_days: int) -> tuple[int, int]:
        sessions_root = settings.data_dir / "sessions"
        if retention_days <= 0 or (not sessions_root.exists()):
            return 0, 0

        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        removed_sessions = 0
        removed_bytes = 0

        for guild_dir in sessions_root.iterdir():
            if not guild_dir.is_dir():
                continue
            for session_dir in guild_dir.iterdir():
                if not session_dir.is_dir():
                    continue
                ts = session_timestamp_utc(session_dir.name)
                if ts is None or ts >= cutoff:
                    continue
                size = 0
                for f in session_dir.rglob("*"):
                    if f.is_file():
                        try:
                            size += f.stat().st_size
                        except OSError:
                            pass
                try:
                    shutil.rmtree(session_dir)
                    removed_sessions += 1
                    removed_bytes += size
                except OSError as exc:
                    logger.warning(
                        "[cleanup] failed_remove session_dir=%s error=%s",
                        session_dir,
                        exc,
                    )
                    continue
        return removed_sessions, removed_bytes

    async def run_startup_cleanup() -> None:
        if not settings.auto_cleanup_enabled or not settings.auto_cleanup_on_start:
            logger.info(
                "[cleanup] startup_skip auto_cleanup_enabled=%s auto_cleanup_on_start=%s",
                settings.auto_cleanup_enabled,
                settings.auto_cleanup_on_start,
            )
            return
        started = time.perf_counter()
        logger.info(
            "[cleanup] startup_begin retention_days=%s data_dir=%s",
            settings.retention_days,
            settings.data_dir,
        )
        removed_sessions, removed_bytes = cleanup_old_sessions(settings.retention_days)
        duration_s = time.perf_counter() - started
        logger.info(
            "[cleanup] startup_done removed_sessions=%s removed_bytes=%s retention_days=%s duration_s=%.3f",
            removed_sessions,
            removed_bytes,
            settings.retention_days,
            duration_s,
        )

    async def recover_unfinished_sessions() -> None:
        if not settings.recovery_auto_post_partial:
            logger.info(
                "[recovery] startup_skip recovery_auto_post_partial=%s",
                settings.recovery_auto_post_partial,
            )
            return

        sessions_root = settings.data_dir / "sessions"
        if not sessions_root.exists():
            logger.info(
                "[recovery] startup_skip reason=no_sessions_root path=%s", sessions_root
            )
            return

        started = time.perf_counter()
        max_to_post = max(1, settings.recovery_max_sessions)
        scanned_guild_dirs = 0
        scanned_session_dirs = 0
        skipped_done = 0
        skipped_already_posted = 0
        skipped_missing_checkpoint = 0
        skipped_invalid_checkpoint = 0
        skipped_no_guild = 0
        skipped_no_channel = 0
        logger.info(
            "[recovery] startup_begin max_sessions=%s sessions_root=%s",
            max_to_post,
            sessions_root,
        )

        posted_count = 0
        for guild_dir in sorted(sessions_root.iterdir(), reverse=True):
            if posted_count >= max_to_post:
                break
            if not guild_dir.is_dir():
                continue
            scanned_guild_dirs += 1
            try:
                guild_id = int(guild_dir.name)
            except ValueError:
                continue

            guild = bot.get_guild(guild_id)
            if guild is None:
                skipped_no_guild += 1
                continue

            channel_id = store.get_chronicle_channel(guild_id)
            if channel_id is None:
                skipped_no_channel += 1
                continue
            maybe_channel = guild.get_channel(channel_id)
            if not isinstance(maybe_channel, discord.TextChannel):
                skipped_no_channel += 1
                continue
            chronicle_channel = maybe_channel

            for session_dir in sorted(guild_dir.iterdir(), reverse=True):
                if posted_count >= max_to_post:
                    break
                if not session_dir.is_dir():
                    continue
                scanned_session_dirs += 1

                checkpoint_path = session_dir / "processing_state.json"
                if not checkpoint_path.exists():
                    skipped_missing_checkpoint += 1
                    continue

                checkpoint = load_json_file(str(checkpoint_path))
                if not checkpoint:
                    skipped_invalid_checkpoint += 1
                    continue
                if checkpoint.get("status") == "done":
                    skipped_done += 1
                    continue
                if checkpoint.get("recovery_posted"):
                    skipped_already_posted += 1
                    continue

                logger.info(
                    "[recovery] posting_partial guild_id=%s session_dir=%s",
                    guild_id,
                    session_dir,
                )
                await try_send(
                    chronicle_channel,
                    (
                        f"Recovered unfinished session: `{session_dir}`\n"
                        "Posting available partial artifacts."
                    ),
                )
                await try_send_file(
                    chronicle_channel,
                    str(checkpoint_path),
                    content="`processing_state.json`",
                )

                artifact_paths = []
                for name in ("full_transcript.txt", "summary.md", "chunk_summaries.md"):
                    p = session_dir / name
                    if p.exists():
                        artifact_paths.append(str(p))

                if artifact_paths:
                    await try_send_files(
                        chronicle_channel,
                        artifact_paths,
                        content="Recovered session artifacts:",
                    )
                else:
                    await try_send(
                        chronicle_channel,
                        "No transcript/summary artifacts were found yet for this unfinished session.",
                    )

                checkpoint["recovery_posted"] = True
                checkpoint["recovery_posted_at_utc"] = datetime.now(UTC).isoformat()
                save_json_file(str(checkpoint_path), checkpoint)
                posted_count += 1
                logger.info(
                    "[recovery] posted_partial guild_id=%s session_dir=%s artifacts=%s posted_count=%s",
                    guild_id,
                    session_dir,
                    len(artifact_paths),
                    posted_count,
                )
        duration_s = time.perf_counter() - started
        logger.info(
            "[recovery] startup_done posted=%s scanned_guild_dirs=%s scanned_session_dirs=%s "
            "skipped_done=%s skipped_already_posted=%s skipped_missing_checkpoint=%s "
            "skipped_invalid_checkpoint=%s skipped_no_guild=%s skipped_no_channel=%s duration_s=%.3f",
            posted_count,
            scanned_guild_dirs,
            scanned_session_dirs,
            skipped_done,
            skipped_already_posted,
            skipped_missing_checkpoint,
            skipped_invalid_checkpoint,
            skipped_no_guild,
            skipped_no_channel,
            duration_s,
        )

    async def wait_voice_ready(
        voice_client: discord.VoiceClient, timeout_s: float = 20.0
    ) -> bool:
        checks = max(1, int(timeout_s * 10))
        for _ in range(checks):
            if voice_client.is_connected() and voice_client.channel is not None:
                return True
            await asyncio.sleep(0.1)
        return False

    def voice_state_snapshot(voice_client: discord.VoiceClient | None) -> str:
        if voice_client is None:
            return "voice_client=None"
        ch = getattr(voice_client, "channel", None)
        ch_id = getattr(ch, "id", None)
        ch_name = getattr(ch, "name", None)
        ws = getattr(voice_client, "ws", None)
        endpoint = getattr(ws, "endpoint", None) if ws else None
        session_id = getattr(ws, "session_id", None) if ws else None
        token = getattr(ws, "token", None) if ws else None
        has_token = bool(token)
        return (
            f"is_connected={voice_client.is_connected()} "
            f"channel_id={ch_id} channel_name={ch_name} "
            f"ws={'yes' if ws else 'no'} endpoint={endpoint} "
            f"session_id={'set' if session_id else 'none'} token={'set' if has_token else 'none'} "
            f"latency={getattr(voice_client, 'latency', 'n/a')}"
        )

    async def start_recording_with_retry(
        voice_client: discord.VoiceClient,
        sink: discord.sinks.Sink,
        done_cb: DoneCallback,
        text_channel: discord.abc.Messageable,
        guild_id: int,
        timeout_s: float = 90.0,
    ) -> None:
        last_error: Exception | None = None
        checks = max(1, int(timeout_s / 1.0))
        for _ in range(checks):
            try:
                voice_client.start_recording(sink, done_cb, text_channel, guild_id)
                return
            except (RecordingException, IndexError, RuntimeError) as exc:
                last_error = exc
                await asyncio.sleep(1.0)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to start recording for unknown reason.")

    def recent_decode_error_count() -> int:
        now = time.monotonic()
        window = max(1, settings.voice_decode_burst_window_seconds)
        while decode_error_events and (now - decode_error_events[0]) > window:
            decode_error_events.popleft()
        return len(decode_error_events)

    async def rotation_loop(
        guild_id: int,
        fallback_channel: discord.abc.Messageable,
        done_cb: DoneCallback,
    ) -> None:
        if settings.recording_rotation_seconds <= 0:
            logger.info("[rotation] disabled guild_id=%s", guild_id)
            return
        logger.info(
            "[rotation] loop_started guild_id=%s interval_s=%s",
            guild_id,
            settings.recording_rotation_seconds,
        )
        while True:
            await asyncio.sleep(settings.recording_rotation_seconds)
            state = guild_state.setdefault(guild_id, GuildRecordingState())
            if state.sink is None or state.processing or state.finalizing:
                logger.info(
                    "[rotation] loop_stopped guild_id=%s sink=%s processing=%s finalizing=%s",
                    guild_id,
                    state.sink is not None,
                    state.processing,
                    state.finalizing,
                )
                return
            guild = bot.get_guild(guild_id)
            if guild is None or guild.voice_client is None:
                logger.warning(
                    "[rotation] skip guild_id=%s reason=voice_client_missing", guild_id
                )
                continue
            await try_send(
                fallback_channel,
                "Rotating recording segment...",
            )
            upsert_active_session(
                guild_id,
                status="rotating",
                voice_channel_id=state.voice_channel_id,
                chronicle_channel_id=store.get_chronicle_channel(guild_id),
                segment_count=state.persisted_segments,
                finalizing=False,
            )
            try:
                state.rotation_triggered += 1
                guild.voice_client.stop_recording()
            except Exception as exc:
                state.rotation_failed += 1
                logger.exception(
                    "[rotation] stop_recording_failed guild_id=%s", guild_id
                )
                await try_send(
                    fallback_channel,
                    f"Rotation trigger failed: `{exc}`",
                )

    async def monitor_voice_health(
        guild_id: int,
        target_voice_channel: VoiceLikeChannel,
        fallback_channel: discord.abc.Messageable,
        done_cb: DoneCallback,
    ) -> None:
        missed_checks = 0
        logger.info(
            "[voice-health] monitor_started guild_id=%s target_voice_channel_id=%s",
            guild_id,
            target_voice_channel.id,
        )
        while True:
            await asyncio.sleep(12.0)
            state = guild_state.setdefault(guild_id, GuildRecordingState())
            if state.sink is None or state.processing:
                logger.info(
                    "[voice-health] monitor_stopped guild_id=%s sink=%s processing=%s",
                    guild_id,
                    state.sink is not None,
                    state.processing,
                )
                return

            guild = bot.get_guild(guild_id)
            if guild is None:
                logger.warning(
                    "[voice-health] monitor_stopped guild_id=%s reason=guild_missing",
                    guild_id,
                )
                return

            voice_client = guild.voice_client
            healthy = (
                voice_client is not None
                and voice_client.is_connected()
                and voice_client.channel is not None
                and voice_client.channel.id == target_voice_channel.id
            )
            if healthy:
                missed_checks = 0
                decode_count = recent_decode_error_count()
                now_mono = time.monotonic()
                if decode_count >= settings.voice_decode_burst_threshold:
                    if now_mono < state.decode_recovery_cooldown_until:
                        state.decode_burst_skipped_cooldown += 1
                        logger.warning(
                            "[voice-health] decode_burst_ignored_cooldown guild_id=%s decode_count=%s cooldown_remaining_s=%.1f",
                            guild_id,
                            decode_count,
                            state.decode_recovery_cooldown_until - now_mono,
                        )
                        continue
                    logger.warning(
                        "[voice-health] decode_burst_detected guild_id=%s decode_count=%s window_s=%s",
                        guild_id,
                        decode_count,
                        settings.voice_decode_burst_window_seconds,
                    )
                    state.decode_burst_triggers += 1
                    state.last_decode_burst_at_utc = datetime.now(UTC).isoformat()
                    state.decode_recovery_cooldown_until = now_mono + max(
                        1, settings.voice_decode_burst_cooldown_seconds
                    )
                    await try_send(
                        fallback_channel,
                        (
                            "Detected voice decode error burst. "
                            "Forcing segment rollover and reconnect..."
                        ),
                    )
                    upsert_active_session(
                        guild_id,
                        status="rotating",
                        voice_channel_id=state.voice_channel_id,
                        chronicle_channel_id=store.get_chronicle_channel(guild_id),
                        segment_count=state.persisted_segments,
                        finalizing=False,
                    )
                    try:
                        state.rotation_triggered += 1
                        voice_client.stop_recording()
                    except Exception as exc:
                        state.rotation_failed += 1
                        logger.exception(
                            "[voice-health] decode_burst_rollover_failed guild_id=%s",
                            guild_id,
                        )
                        await try_send(
                            fallback_channel,
                            f"Decode burst recovery failed: `{exc}`",
                        )
                continue

            missed_checks += 1
            if missed_checks < 3:
                continue
            missed_checks = 0
            logger.warning(
                "[voice-health] unstable guild_id=%s target_voice_channel_id=%s",
                guild_id,
                target_voice_channel.id,
            )

            await try_send(
                fallback_channel,
                "Voice connection looks unstable. Attempting automatic reconnect...",
            )
            if state.restart_in_progress:
                logger.info(
                    "[voice-health] reconnect_skipped guild_id=%s reason=in_progress",
                    guild_id,
                )
                continue
            state.restart_in_progress = True
            try:
                state.reconnect_attempts += 1
                previous_sink = state.sink
                if previous_sink is None:
                    raise RuntimeError("Active recording sink not found.")
                recovered_client = await connect_voice_with_retry(
                    guild, target_voice_channel, attempts=3
                )
                await asyncio.sleep(1.5)
                next_sink = discord.sinks.WaveSink()
                await start_recording_with_retry(
                    voice_client=recovered_client,
                    sink=next_sink,
                    done_cb=done_cb,
                    text_channel=fallback_channel,
                    guild_id=guild_id,
                    timeout_s=20.0,
                )
                persisted = await persist_recording_segment(
                    guild,
                    state,
                    previous_sink,
                    fallback_channel=fallback_channel,
                )
                state.sink = next_sink
                upsert_active_session(
                    guild_id,
                    status="recording",
                    voice_channel_id=target_voice_channel.id,
                    chronicle_channel_id=store.get_chronicle_channel(guild_id),
                    segment_count=state.persisted_segments + 1,
                    finalizing=False,
                )
                await try_send(
                    fallback_channel,
                    (
                        "Voice connection recovered. Recording resumed in a new segment."
                        if persisted
                        else "Voice connection recovered. Recording resumed."
                    ),
                )
                state.reconnect_successes += 1
                logger.info(
                    "[voice-health] recovered guild_id=%s persisted_previous_sink=%s",
                    guild_id,
                    persisted,
                )
            except Exception as exc:
                state.reconnect_failures += 1
                logger.exception(
                    "[voice-health] reconnect_failed guild_id=%s", guild_id
                )
                await try_send(
                    fallback_channel,
                    f"Reconnect failed, but last segment is saved to disk: `{exc}`. Will retry automatically.",
                )
            finally:
                state.restart_in_progress = False

    async def connect_voice_with_retry(
        guild: discord.Guild,
        voice_channel: VoiceLikeChannel,
        attempts: int = 2,
    ) -> discord.VoiceClient:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                current = guild.voice_client
                if current is not None:
                    # Recreate stale/disconnected clients instead of reusing them.
                    if (
                        (not current.is_connected())
                        or current.channel is None
                        or current.channel.id != voice_channel.id
                    ):
                        await current.disconnect(force=True)
                        await asyncio.sleep(0.5)
                        current = None
                if current is None:
                    current = await voice_channel.connect()
                return current
            except Exception as exc:
                last_error = exc
                await asyncio.sleep(1.0)
        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to establish voice connection.")

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
        issues = config_doctor_issues(settings)
        if issues:
            logger.warning(
                "[config-doctor] detected %s potential issue(s):", len(issues)
            )
            for issue in issues:
                logger.warning("[config-doctor] %s", issue)
        else:
            logger.info("[config-doctor] no obvious config issues detected")
        removed_stale = prune_stale_active_sessions(max_age_seconds=300)
        if removed_stale > 0:
            logger.info(
                "[runtime] pruned %s stale active session entrie(s)", removed_stale
            )
        runtime_state = load_runtime_state()
        active_count = len(runtime_state.get("active_sessions", {}))
        if active_count > 0:
            logger.info(
                "[runtime] detected %s active session entries from previous run",
                active_count,
            )
        await run_startup_cleanup()
        await recover_unfinished_sessions()
        ok, details = await whisper.warmup()
        logger.info("[whisper] warmup status=%s details=%s", ok, details)
        ok, details = await llm.warmup()
        logger.info("[llm] warmup status=%s details=%s", ok, details)

    async def resolve_invoking_member(
        ctx: discord.ApplicationContext,
    ) -> discord.Member | None:
        if ctx.guild is None or ctx.user is None:
            return None
        if isinstance(ctx.author, discord.Member):
            return ctx.author
        if isinstance(ctx.user, discord.Member):
            return ctx.user

        member = ctx.guild.get_member(ctx.user.id)
        if member is not None:
            return member
        try:
            return await ctx.guild.fetch_member(ctx.user.id)
        except Exception:
            return None

    def _as_voice_like(channel: object | None) -> VoiceLikeChannel | None:
        if isinstance(channel, discord.VoiceChannel):
            return channel
        return None

    async def resolve_invoking_voice_channel(
        ctx: discord.ApplicationContext,
    ) -> VoiceLikeChannel | None:
        member = await resolve_invoking_member(ctx)
        if member and member.voice:
            voice_like = _as_voice_like(member.voice.channel)
            if voice_like is not None:
                return voice_like

        if ctx.guild is None or ctx.user is None:
            return None

        # Fallback path: read raw guild voice_states cache directly.
        voice_states = getattr(ctx.guild, "voice_states", None)
        if isinstance(voice_states, dict):
            state = voice_states.get(ctx.user.id)
            if state is None:
                state = voice_states.get(str(ctx.user.id))
            if state is not None:
                channel = getattr(state, "channel", None)
                voice_like = _as_voice_like(channel)
                if voice_like is not None:
                    return voice_like
                channel_id = getattr(state, "channel_id", None)
                if isinstance(channel_id, int):
                    maybe = ctx.guild.get_channel(channel_id)
                    voice_like = _as_voice_like(maybe)
                    if voice_like is not None:
                        return voice_like

        for channel in ctx.guild.voice_channels:
            if any(member_obj.id == ctx.user.id for member_obj in channel.members):
                return channel
        return None

    def resolve_text_channel(
        ctx: discord.ApplicationContext, raw_channel: object | None
    ) -> discord.TextChannel | None:
        if ctx.guild is None:
            return None

        resolved_channel: discord.TextChannel | None = None
        channel = raw_channel

        if isinstance(channel, discord.TextChannel):
            resolved_channel = channel
        elif hasattr(channel, "id"):
            maybe = ctx.guild.get_channel(int(channel.id))
            if isinstance(maybe, discord.TextChannel):
                resolved_channel = maybe
        elif isinstance(channel, str):
            channel_value = channel.strip()
            if channel_value.startswith("#"):
                channel_value = channel_value[1:].strip()

            if channel_value:
                exact_matches = [
                    ch
                    for ch in ctx.guild.text_channels
                    if ch.name.casefold() == channel_value.casefold()
                ]
                if len(exact_matches) == 1:
                    resolved_channel = exact_matches[0]

            match = re.search(r"\d{15,22}", channel)
            if match:
                maybe = ctx.guild.get_channel(int(match.group(0)))
                if isinstance(maybe, discord.TextChannel):
                    resolved_channel = maybe

        # Practical fallback for stale slash schema: use current text channel.
        if resolved_channel is None and isinstance(ctx.channel, discord.TextChannel):
            resolved_channel = ctx.channel

        return resolved_channel

    def resolve_voice_channel(
        ctx: discord.ApplicationContext, raw_channel: object | None
    ) -> VoiceLikeChannel | None:
        if ctx.guild is None:
            return None

        resolved_channel: VoiceLikeChannel | None = None
        channel = raw_channel

        voice_like = _as_voice_like(channel)
        if voice_like is not None:
            resolved_channel = voice_like
        elif hasattr(channel, "id"):
            maybe = ctx.guild.get_channel(int(channel.id))
            voice_like = _as_voice_like(maybe)
            if voice_like is not None:
                resolved_channel = voice_like
        elif isinstance(channel, str):
            channel_value = channel.strip()
            if channel_value:
                exact_matches = [
                    ch
                    for ch in ctx.guild.voice_channels
                    if ch.name.casefold() == channel_value.casefold()
                ]
                if len(exact_matches) == 1:
                    resolved_channel = exact_matches[0]

            match = re.search(r"\d{15,22}", channel)
            if match:
                maybe = ctx.guild.get_channel(int(match.group(0)))
                voice_like = _as_voice_like(maybe)
                if voice_like is not None:
                    resolved_channel = voice_like

        return resolved_channel

    async def require_manage_guild(ctx: discord.ApplicationContext) -> bool:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return False
        if isinstance(ctx.author, discord.Member):
            perms = ctx.author.guild_permissions
            if perms.administrator or perms.manage_guild:
                return True
        await ctx.respond(
            "You need `Manage Server` permission to run this command.", ephemeral=True
        )
        return False

    @bot.slash_command(
        name="chronicle_cleanup_now",
        description="Delete old session artifacts by retention policy",
    )
    async def chronicle_cleanup_now(ctx: discord.ApplicationContext) -> None:
        if not await require_manage_guild(ctx):
            return
        if not settings.auto_cleanup_enabled:
            await ctx.respond(
                "Cleanup is disabled (`AUTO_CLEANUP_ENABLED=false`).", ephemeral=True
            )
            return
        started = time.perf_counter()
        removed_sessions, removed_bytes = cleanup_old_sessions(settings.retention_days)
        duration_s = time.perf_counter() - started
        logger.info(
            "[cleanup] manual_done guild_id=%s removed_sessions=%s removed_bytes=%s retention_days=%s duration_s=%.3f",
            ctx.guild.id if ctx.guild else "none",
            removed_sessions,
            removed_bytes,
            settings.retention_days,
            duration_s,
        )
        await ctx.respond(
            (
                f"Cleanup done.\n"
                f"Retention days: `{settings.retention_days}`\n"
                f"Removed sessions: `{removed_sessions}`\n"
                f"Freed bytes: `{removed_bytes}`"
            ),
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_purge_session", description="Delete one saved session by ID"
    )
    async def chronicle_purge_session(
        ctx: discord.ApplicationContext,
        session_id: str = discord.Option(
            str,
            description="Session folder id, e.g. 20260219_201349",
            autocomplete=session_autocomplete,
            required=True,
        ),
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if not settings.allow_purge_commands:
            await ctx.respond(
                "Purge commands are disabled (`ALLOW_PURGE_COMMANDS=false`).",
                ephemeral=True,
            )
            return
        if ctx.guild is None:
            return
        guild_sessions_dir = settings.data_dir / "sessions" / str(ctx.guild.id)
        session_dir = guild_sessions_dir / session_id.strip()
        if not session_dir.exists() or not session_dir.is_dir():
            await ctx.respond(f"Session `{session_id}` not found.", ephemeral=True)
            return
        try:
            shutil.rmtree(session_dir)
        except OSError as exc:
            await ctx.respond(
                f"Failed to delete `{session_id}`: `{exc}`", ephemeral=True
            )
            return
        await ctx.respond(f"Session `{session_id}` deleted.", ephemeral=True)

    @bot.slash_command(
        name="chronicle_purge_guild_data",
        description="Delete all saved data for this guild",
    )
    async def chronicle_purge_guild_data(
        ctx: discord.ApplicationContext,
        confirm: str = discord.Option(
            str,
            description="Type PURGE to confirm",
            required=True,
        ),
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if not settings.allow_purge_commands:
            await ctx.respond(
                "Purge commands are disabled (`ALLOW_PURGE_COMMANDS=false`).",
                ephemeral=True,
            )
            return
        if ctx.guild is None:
            return
        if confirm.strip() != "PURGE":
            await ctx.respond(
                "Confirmation failed. Type exactly `PURGE`.", ephemeral=True
            )
            return
        guild_sessions_dir = settings.data_dir / "sessions" / str(ctx.guild.id)
        if not guild_sessions_dir.exists():
            await ctx.respond("No saved session data for this guild.", ephemeral=True)
            return
        try:
            shutil.rmtree(guild_sessions_dir)
        except OSError as exc:
            await ctx.respond(f"Failed to purge guild data: `{exc}`", ephemeral=True)
            return
        await ctx.respond(
            "All saved guild session data has been deleted.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_setup", description="Set text channel for chronicle reports"
    )
    async def chronicle_setup(
        ctx: discord.ApplicationContext,
        channel: discord.TextChannel = discord.Option(
            input_type=discord.SlashCommandOptionType.channel,
            description="Text channel for transcript/summary posts",
            channel_types=[discord.ChannelType.text],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        resolved_channel = resolve_text_channel(ctx, channel)

        if resolved_channel is None:
            await ctx.respond(
                "Could not resolve a text channel. Use this command in the target text channel or pass #channel.",
                ephemeral=True,
            )
            return

        store.set_chronicle_channel(ctx.guild.id, resolved_channel.id)
        await ctx.respond(
            f"Chronicle channel set to {resolved_channel.mention}.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_setup_here",
        description="Set current text channel for chronicle reports",
    )
    async def chronicle_setup_here(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.respond(
                "Run this command from a server text channel.", ephemeral=True
            )
            return
        store.set_chronicle_channel(ctx.guild.id, ctx.channel.id)
        await ctx.respond(
            f"Chronicle channel set to {ctx.channel.mention}.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_setup_voice",
        description="Set default voice channel for recording",
    )
    async def chronicle_setup_voice(
        ctx: discord.ApplicationContext,
        channel: discord.VoiceChannel = discord.Option(
            input_type=discord.SlashCommandOptionType.channel,
            description="Voice channel for recording",
            channel_types=[discord.ChannelType.voice],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        resolved_channel = resolve_voice_channel(ctx, channel)

        if resolved_channel is None:
            await ctx.respond(
                "Could not resolve selected voice channel.",
                ephemeral=True,
            )
            return

        store.set_voice_channel(ctx.guild.id, resolved_channel.id)
        await ctx.respond(
            f"Default voice channel set to {resolved_channel.mention}.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_setup_channels",
        description="Set both voice and transcript text channels",
    )
    async def chronicle_setup_channels(
        ctx: discord.ApplicationContext,
        voice_channel: discord.VoiceChannel = discord.Option(
            input_type=discord.SlashCommandOptionType.channel,
            description="Voice channel for recording",
            channel_types=[discord.ChannelType.voice],
            required=True,
        ),
        transcript_channel: discord.TextChannel = discord.Option(
            input_type=discord.SlashCommandOptionType.channel,
            description="Text channel for transcript/summary posts",
            channel_types=[discord.ChannelType.text],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        resolved_voice = resolve_voice_channel(ctx, voice_channel)
        resolved_text = resolve_text_channel(ctx, transcript_channel)
        if resolved_voice is None or resolved_text is None:
            await ctx.respond(
                "Could not resolve one or both channels from the selected values.",
                ephemeral=True,
            )
            return

        store.set_voice_channel(ctx.guild.id, resolved_voice.id)
        store.set_chronicle_channel(ctx.guild.id, resolved_text.id)
        await ctx.respond(
            f"Setup complete.\nVoice: {resolved_voice.mention}\nTranscript: {resolved_text.mention}",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_defaults_language",
        description="Set guild default summary language (campaign fallback)",
    )
    async def chronicle_defaults_language(
        ctx: discord.ApplicationContext,
        language: str = discord.Option(
            str,
            description="Default summary language",
            choices=["en", "uk", "ru"],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        store.set_default_summary_language(ctx.guild.id, language)
        await ctx.respond(
            f"Guild default summary language set to `{language}`.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_defaults_context",
        description="Set guild default DM context text (campaign fallback)",
    )
    async def chronicle_defaults_context(
        ctx: discord.ApplicationContext,
        context: str = discord.Option(
            str,
            description="Default session intro/background context",
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        store.set_default_session_context(ctx.guild.id, context)
        await ctx.respond(
            f"Guild default context saved (`{len(context.strip())}` chars).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_defaults_names",
        description="Set guild default names/roles hints (campaign fallback)",
    )
    async def chronicle_defaults_names(
        ctx: discord.ApplicationContext,
        hints: str = discord.Option(
            str,
            description="Default canonical names and roles",
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        store.set_default_name_hints(ctx.guild.id, hints)
        await ctx.respond(
            f"Guild default name hints saved (`{len(hints.strip())}` chars).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_create",
        description="Create a campaign and optionally set initial language",
    )
    async def chronicle_campaign_create(
        ctx: discord.ApplicationContext,
        name: str = discord.Option(str, description="Campaign name", required=True),
        language: str = discord.Option(
            str,
            description="Optional campaign summary language override",
            choices=["en", "uk", "ru"],
            required=False,
            default="",
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        try:
            campaign = store.create_campaign(
                ctx.guild.id, name=name, summary_language=language
            )
        except ValueError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return
        await ctx.respond(
            f"Campaign created: `{campaign['name']}` (`{campaign['id']}`).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_list",
        description="List campaigns for this guild",
    )
    async def chronicle_campaign_list(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        campaigns = store.list_campaigns(ctx.guild.id)
        active_id = store.get_active_campaign_id(ctx.guild.id)
        if not campaigns:
            await ctx.respond(
                "No campaigns found. Use `/chronicle_campaign_create` first.",
                ephemeral=True,
            )
            return
        lines = ["## Campaigns"]
        for c in campaigns:
            marker = " (active)" if c.get("id") == active_id else ""
            lines.append(f"- `{c.get('id')}`: **{c.get('name')}**{marker}")
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(
        name="chronicle_campaign_use",
        description="Set active campaign for next recordings",
    )
    async def chronicle_campaign_use(
        ctx: discord.ApplicationContext,
        campaign: str = discord.Option(
            str,
            description="Campaign id or exact name",
            autocomplete=campaign_autocomplete,
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        resolved = store.find_campaign(ctx.guild.id, campaign)
        if not resolved:
            await ctx.respond(
                f"Campaign not found: `{campaign}`",
                ephemeral=True,
            )
            return
        try:
            store.set_active_campaign(ctx.guild.id, str(resolved["id"]))
        except ValueError as exc:
            await ctx.respond(str(exc), ephemeral=True)
            return
        await ctx.respond(
            f"Active campaign set to `{resolved['name']}` (`{resolved['id']}`).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_show",
        description="Show active campaign effective settings",
    )
    async def chronicle_campaign_show(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        resolved = store.resolve_active_campaign_settings(ctx.guild.id)
        campaign_id = resolved.get("campaign_id", "")
        if not campaign_id:
            await ctx.respond(
                "No active campaign selected. Use `/chronicle_campaign_create` and `/chronicle_campaign_use`.",
                ephemeral=True,
            )
            return
        lines = ["## Active Campaign"]
        lines.append(
            f"- Campaign: `{resolved.get('campaign_name', '')}` (`{campaign_id}`)"
        )
        lines.append(
            f"- Effective language: `{resolved.get('summary_language', 'ru')}`"
        )
        lines.append(
            f"- Effective session context: `{'set' if resolved.get('session_context') else 'not set'}` ({len(resolved.get('session_context', ''))} chars)"
        )
        lines.append(
            f"- Effective name hints: `{'set' if resolved.get('name_hints') else 'not set'}` ({len(resolved.get('name_hints', ''))} chars)"
        )
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(
        name="chronicle_campaign_context",
        description="Update active campaign context text",
    )
    async def chronicle_campaign_update_context(
        ctx: discord.ApplicationContext,
        context: str = discord.Option(
            str,
            description="Campaign session intro/background context",
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        campaign_id = store.get_active_campaign_id(ctx.guild.id)
        if not campaign_id:
            await ctx.respond(
                "No active campaign selected.",
                ephemeral=True,
            )
            return
        store.update_campaign(ctx.guild.id, campaign_id, session_context=context)
        await ctx.respond(
            f"Campaign context updated (`{len(context.strip())}` chars).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_names",
        description="Update active campaign names/roles hints",
    )
    async def chronicle_campaign_update_names(
        ctx: discord.ApplicationContext,
        hints: str = discord.Option(
            str,
            description="Canonical names and roles",
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        campaign_id = store.get_active_campaign_id(ctx.guild.id)
        if not campaign_id:
            await ctx.respond(
                "No active campaign selected.",
                ephemeral=True,
            )
            return
        store.update_campaign(ctx.guild.id, campaign_id, name_hints=hints)
        await ctx.respond(
            f"Campaign name hints updated (`{len(hints.strip())}` chars).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_language",
        description="Update active campaign summary language override",
    )
    async def chronicle_campaign_update_language(
        ctx: discord.ApplicationContext,
        language: str = discord.Option(
            str,
            description="Campaign summary language override",
            choices=["en", "uk", "ru"],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        campaign_id = store.get_active_campaign_id(ctx.guild.id)
        if not campaign_id:
            await ctx.respond(
                "No active campaign selected.",
                ephemeral=True,
            )
            return
        store.update_campaign(ctx.guild.id, campaign_id, summary_language=language)
        await ctx.respond(
            f"Campaign language override set to `{language}`.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_campaign_lang_clear",
        description="Clear active campaign language override (use guild default)",
    )
    async def chronicle_campaign_language_clear(
        ctx: discord.ApplicationContext,
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        campaign_id = store.get_active_campaign_id(ctx.guild.id)
        if not campaign_id:
            await ctx.respond(
                "No active campaign selected.",
                ephemeral=True,
            )
            return
        store.update_campaign(ctx.guild.id, campaign_id, summary_language="")
        await ctx.respond(
            "Campaign language override cleared (will use guild default).",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_status", description="Show recorder status and health counters"
    )
    async def chronicle_status(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        configured_voice_id = store.get_voice_channel(ctx.guild.id)
        configured_voice = (
            ctx.guild.get_channel(configured_voice_id) if configured_voice_id else None
        )
        configured_text_id = store.get_chronicle_channel(ctx.guild.id)
        configured_text = (
            ctx.guild.get_channel(configured_text_id) if configured_text_id else None
        )
        active_campaign_id = store.get_active_campaign_id(ctx.guild.id)
        active_campaign = (
            store.get_campaign(ctx.guild.id, active_campaign_id)
            if active_campaign_id
            else None
        )
        guild_default_language = store.get_default_summary_language(
            ctx.guild.id, default="ru"
        )
        voice_client = ctx.guild.voice_client
        lines = ["## Chronicle Status"]
        lines.append(f"- Recording active: `{state.sink is not None}`")
        lines.append(f"- Processing active: `{state.processing}`")
        lines.append(f"- Finalizing: `{state.finalizing}`")
        lines.append(
            f"- Configured voice channel: `{getattr(configured_voice, 'name', 'not set')}`"
        )
        lines.append(
            f"- Configured chronicle channel: `{getattr(configured_text, 'name', 'not set')}`"
        )
        lines.append(
            f"- Active campaign: `{active_campaign.get('name') if active_campaign else 'not set'}` (`{active_campaign_id or ''}`)"
        )
        lines.append(f"- Guild default language: `{guild_default_language}`")
        if voice_client is not None:
            lines.append(
                f"- Bot voice connection: `connected={voice_client.is_connected()} channel={getattr(voice_client.channel, 'name', 'none')}`"
            )
        else:
            lines.append("- Bot voice connection: `none`")
        if state.started_at_utc is not None:
            elapsed_s = (datetime.now(UTC) - state.started_at_utc).total_seconds()
            lines.append(f"- Session wall time: `{elapsed_s:.1f}s`")
        lines.append(
            f"- Rotation counters: `triggered={state.rotation_triggered}, resumed={state.rotation_resumed}, failed={state.rotation_failed}`"
        )
        lines.append(
            f"- Reconnect counters: `attempts={state.reconnect_attempts}, success={state.reconnect_successes}, failed={state.reconnect_failures}`"
        )
        lines.append(
            (
                "- Decode burst counters: "
                f"`triggers={state.decode_burst_triggers}, cooldown_skips={state.decode_burst_skipped_cooldown}, "
                f"recent_errors={recent_decode_error_count()}`"
            )
        )
        lines.append(
            (
                "- Decode burst config: "
                f"`window={settings.voice_decode_burst_window_seconds}s, "
                f"threshold={settings.voice_decode_burst_threshold}, "
                f"cooldown={settings.voice_decode_burst_cooldown_seconds}s`"
            )
        )
        if state.last_decode_burst_at_utc:
            lines.append(f"- Last decode burst: `{state.last_decode_burst_at_utc}`")
        metrics_snapshot = metrics.snapshot()
        interesting_stages = (
            "session_process",
            "session_reprocess",
            "asr_transcribe",
            "llm_summarize",
            "audio_compress",
            "audio_mix",
            "discord_publish",
        )
        for stage in interesting_stages:
            stage_metrics = metrics_snapshot.get(stage)
            if not stage_metrics:
                continue
            lines.append(
                (
                    f"- Metrics `{stage}`: "
                    f"`calls={stage_metrics['calls']}, "
                    f"errors={stage_metrics['errors']}, "
                    f"avg={stage_metrics['avg_latency_s']:.2f}s, "
                    f"max={stage_metrics['max_latency_s']:.2f}s`"
                )
            )
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(
        name="chronicle_reprocess_last",
        description="Reprocess the latest saved session for this guild",
    )
    async def chronicle_reprocess_last(ctx: discord.ApplicationContext) -> None:
        if not await require_manage_guild(ctx):
            return
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send(
                "Cannot reprocess while recording is active.", ephemeral=True
            )
            return
        if state.processing:
            await ctx.followup.send(
                "Another processing task is already running.", ephemeral=True
            )
            return

        session_dir = latest_session_dir_for_guild(ctx.guild.id)
        if session_dir is None:
            await ctx.followup.send(
                "No saved sessions found for this guild.", ephemeral=True
            )
            return

        chronicle_channel_id = store.get_chronicle_channel(ctx.guild.id)
        target_channel: discord.abc.Messageable | None = None
        if chronicle_channel_id is not None:
            maybe = ctx.guild.get_channel(chronicle_channel_id)
            if isinstance(maybe, discord.TextChannel):
                target_channel = maybe
        if target_channel is None:
            target_channel = ctx.channel

        state.processing = True
        started = time.perf_counter()
        snapshot = read_session_snapshot(session_dir)
        summary_language = snapshot.get("summary_language", "ru")
        session_context = snapshot.get("session_context", "")
        name_hints = snapshot.get("name_hints", "")
        await try_send(
            target_channel,
            f"Reprocessing latest saved session: `{session_dir.name}` (language `{summary_language}`)...",
        )
        try:
            artifacts = await asyncio.wait_for(
                processor.reprocess_saved_session(
                    session_dir=session_dir,
                    summary_language=summary_language,
                    session_context=session_context,
                    name_hints=name_hints,
                    campaign_id=snapshot.get("campaign_id", ""),
                    campaign_name=snapshot.get("campaign_name", ""),
                ),
                timeout=settings.processing_timeout_seconds,
            )
            await try_send(target_channel, f"Reprocess done: `{artifacts.session_dir}`")
            await try_send_file(
                target_channel,
                str(artifacts.full_transcript_txt_path),
                content="## Full Transcript (attached as .txt)",
            )
            if artifacts.mixed_audio_path and artifacts.mixed_audio_path.exists():
                await try_send_file(
                    target_channel,
                    str(artifacts.mixed_audio_path),
                    content="## Mixed Session Audio (.mp3)",
                )
            elif settings.publish_per_speaker_audio:
                mp3_paths = [
                    str(item.audio_path)
                    for item in artifacts.speaker_transcripts
                    if item.audio_path.suffix.lower() == ".mp3"
                    and item.audio_path.exists()
                ]
                if mp3_paths:
                    await try_send_files(
                        target_channel,
                        mp3_paths,
                        content="## Audio Tracks (.mp3)",
                    )
            await try_send(target_channel, "## AI Session Summary")
            await send_long(target_channel, artifacts.summary_markdown)
            logger.info(
                "[reprocess] command_done guild_id=%s session_dir=%s duration_s=%.3f",
                ctx.guild.id,
                session_dir,
                time.perf_counter() - started,
            )
            await ctx.followup.send(
                f"Reprocessed `{session_dir.name}` successfully.", ephemeral=True
            )
        except TimeoutError:
            await try_send(
                target_channel, "Reprocess timed out. Check Whisper/LLM availability."
            )
            await ctx.followup.send("Reprocess timed out.", ephemeral=True)
        except Exception as exc:
            logger.exception(
                "[reprocess] command_failed guild_id=%s session_dir=%s",
                ctx.guild.id,
                session_dir,
            )
            await try_send(target_channel, f"Reprocess failed: `{exc}`")
            await ctx.followup.send(f"Reprocess failed: `{exc}`", ephemeral=True)
        finally:
            state.processing = False

    @bot.slash_command(
        name="chronicle_sessions",
        description="List recent sessions for this guild",
    )
    async def chronicle_sessions(
        ctx: discord.ApplicationContext,
        limit: int = discord.Option(
            int,
            description="How many recent sessions to list",
            required=False,
            default=10,
            min_value=1,
            max_value=50,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        session_dirs = list_session_dirs_for_guild(ctx.guild.id, limit=limit)
        if not session_dirs:
            await ctx.respond("No sessions found.", ephemeral=True)
            return
        lines = ["## Sessions"]
        for session_dir in session_dirs:
            snapshot = read_session_snapshot(session_dir)
            campaign_name = snapshot.get("campaign_name") or "n/a"
            lines.append(
                f"- `{session_dir.name}` campaign=`{campaign_name}` language=`{snapshot.get('summary_language', 'ru')}`"
            )
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(
        name="chronicle_reprocess",
        description="Reprocess a specific session by ID",
    )
    async def chronicle_reprocess(
        ctx: discord.ApplicationContext,
        session_id: str = discord.Option(
            str,
            description="Session folder id, e.g. 20260219_201349",
            autocomplete=session_autocomplete,
            required=True,
        ),
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send(
                "Cannot reprocess while recording is active.", ephemeral=True
            )
            return
        if state.processing:
            await ctx.followup.send(
                "Another processing task is already running.", ephemeral=True
            )
            return

        session_dir = (
            settings.data_dir / "sessions" / str(ctx.guild.id) / session_id.strip()
        )
        if not session_dir.exists() or not session_dir.is_dir():
            await ctx.followup.send(
                f"Session `{session_id}` not found.", ephemeral=True
            )
            return

        chronicle_channel_id = store.get_chronicle_channel(ctx.guild.id)
        target_channel: discord.abc.Messageable | None = None
        if chronicle_channel_id is not None:
            maybe = ctx.guild.get_channel(chronicle_channel_id)
            if isinstance(maybe, discord.TextChannel):
                target_channel = maybe
        if target_channel is None:
            target_channel = ctx.channel

        snapshot = read_session_snapshot(session_dir)
        state.processing = True
        try:
            await try_send(
                target_channel,
                f"Reprocessing session `{session_id}` (language `{snapshot.get('summary_language', 'ru')}`)...",
            )
            artifacts = await asyncio.wait_for(
                processor.reprocess_saved_session(
                    session_dir=session_dir,
                    summary_language=snapshot.get("summary_language", "ru"),
                    session_context=snapshot.get("session_context", ""),
                    name_hints=snapshot.get("name_hints", ""),
                    campaign_id=snapshot.get("campaign_id", ""),
                    campaign_name=snapshot.get("campaign_name", ""),
                ),
                timeout=settings.processing_timeout_seconds,
            )
            await try_send(target_channel, f"Reprocess done: `{artifacts.session_dir}`")
            await try_send_file(
                target_channel,
                str(artifacts.full_transcript_txt_path),
                content="## Full Transcript (attached as .txt)",
            )
            await try_send(target_channel, "## AI Session Summary")
            await send_long(target_channel, artifacts.summary_markdown)
            await ctx.followup.send(
                f"Reprocessed `{session_id}` successfully.", ephemeral=True
            )
        except TimeoutError:
            await try_send(
                target_channel, "Reprocess timed out. Check Whisper/LLM availability."
            )
            await ctx.followup.send("Reprocess timed out.", ephemeral=True)
        except Exception as exc:
            logger.exception(
                "[reprocess] command_failed guild_id=%s session_dir=%s",
                ctx.guild.id,
                session_dir,
            )
            await try_send(target_channel, f"Reprocess failed: `{exc}`")
            await ctx.followup.send(f"Reprocess failed: `{exc}`", ephemeral=True)
        finally:
            state.processing = False

    @bot.slash_command(
        name="chronicle_session_move",
        description="Move a session to another campaign",
    )
    async def chronicle_session_move(
        ctx: discord.ApplicationContext,
        session_id: str = discord.Option(
            str,
            description="Session folder id, e.g. 20260219_201349",
            autocomplete=session_autocomplete,
            required=True,
        ),
        campaign: str = discord.Option(
            str,
            description="Target campaign id or exact name",
            autocomplete=campaign_autocomplete,
            required=True,
        ),
        reprocess: bool = discord.Option(
            bool,
            description="Reprocess summary after move",
            required=False,
            default=False,
        ),
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)
        session_dir = (
            settings.data_dir / "sessions" / str(ctx.guild.id) / session_id.strip()
        )
        checkpoint_path = session_dir / "processing_state.json"
        if not checkpoint_path.exists():
            await ctx.followup.send(
                f"Session `{session_id}` has no `processing_state.json`.",
                ephemeral=True,
            )
            return
        target_campaign = store.find_campaign(ctx.guild.id, campaign)
        if not target_campaign:
            await ctx.followup.send(
                f"Campaign not found: `{campaign}`",
                ephemeral=True,
            )
            return
        snapshot = load_json_file(str(checkpoint_path)) or {}
        if not isinstance(snapshot, dict):
            snapshot = {}
        snapshot["campaign_id"] = str(target_campaign.get("id") or "")
        snapshot["campaign_name"] = str(target_campaign.get("name") or "")
        snapshot["moved_at_utc"] = datetime.now(UTC).isoformat()
        snapshot["moved_by_user_id"] = int(ctx.user.id)
        save_json_file(str(checkpoint_path), snapshot)

        if not reprocess:
            await ctx.followup.send(
                f"Session `{session_id}` moved to campaign `{target_campaign.get('name')}`.",
                ephemeral=True,
            )
            return

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.processing or state.sink is not None:
            await ctx.followup.send(
                "Session moved, but reprocess skipped because recorder is busy.",
                ephemeral=True,
            )
            return

        chronicle_channel_id = store.get_chronicle_channel(ctx.guild.id)
        target_channel: discord.abc.Messageable | None = None
        if chronicle_channel_id is not None:
            maybe = ctx.guild.get_channel(chronicle_channel_id)
            if isinstance(maybe, discord.TextChannel):
                target_channel = maybe
        if target_channel is None:
            target_channel = ctx.channel

        state.processing = True
        try:
            session_snapshot = read_session_snapshot(session_dir)
            artifacts = await asyncio.wait_for(
                processor.reprocess_saved_session(
                    session_dir=session_dir,
                    summary_language=session_snapshot.get("summary_language", "ru"),
                    session_context=session_snapshot.get("session_context", ""),
                    name_hints=session_snapshot.get("name_hints", ""),
                    campaign_id=session_snapshot.get("campaign_id", ""),
                    campaign_name=session_snapshot.get("campaign_name", ""),
                ),
                timeout=settings.processing_timeout_seconds,
            )
            await try_send(
                target_channel,
                f"Session `{session_id}` moved and reprocessed in campaign `{target_campaign.get('name')}`.",
            )
            await try_send_file(
                target_channel,
                str(artifacts.full_transcript_txt_path),
                content="## Full Transcript (attached as .txt)",
            )
            await try_send(target_channel, "## AI Session Summary")
            await send_long(target_channel, artifacts.summary_markdown)
            await ctx.followup.send(
                f"Session `{session_id}` moved and reprocessed.",
                ephemeral=True,
            )
        except Exception as exc:
            await ctx.followup.send(
                f"Session moved, but reprocess failed: `{exc}`",
                ephemeral=True,
            )
        finally:
            state.processing = False

    async def _run_campaign_summarize(
        ctx: discord.ApplicationContext,
        *,
        campaign: str,
        from_date: str = "",
        to_date: str = "",
        limit: int = 500,
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)

        resolved_campaign: dict[str, Any] | None
        if campaign.strip():
            resolved_campaign = store.find_campaign(ctx.guild.id, campaign)
        else:
            active_id = store.get_active_campaign_id(ctx.guild.id)
            resolved_campaign = (
                store.get_campaign(ctx.guild.id, active_id) if active_id else None
            )
        if not resolved_campaign:
            await ctx.followup.send(
                "Campaign not found. Select active campaign or pass campaign id/name.",
                ephemeral=True,
            )
            return

        from_date = from_date.strip()
        to_date = to_date.strip()
        parsed_from = parse_yyyy_mm_dd(from_date) if from_date else None
        parsed_to = parse_yyyy_mm_dd(to_date) if to_date else None
        if (from_date.strip() and not parsed_from) or (
            to_date.strip() and not parsed_to
        ):
            await ctx.followup.send(
                "Invalid date format. Use YYYY-MM-DD.",
                ephemeral=True,
            )
            return
        if parsed_from and parsed_to and parsed_from > parsed_to:
            await ctx.followup.send(
                "`from_date` must be <= `to_date`.",
                ephemeral=True,
            )
            return

        campaign_id = str(resolved_campaign.get("id") or "")
        campaign_name = str(resolved_campaign.get("name") or "")
        sessions = list_session_dirs_for_campaign(
            ctx.guild.id,
            campaign_id,
            from_date=parsed_from,
            to_date=parsed_to,
            limit=max(1, limit),
        )
        if not sessions:
            suffix = " in selected range" if (parsed_from or parsed_to) else ""
            await ctx.followup.send(
                f"No sessions found for campaign `{campaign_name}`{suffix}.",
                ephemeral=True,
            )
            return

        guild_default_language = store.get_default_summary_language(
            ctx.guild.id, default="ru"
        )
        guild_default_context = store.get_default_session_context(
            ctx.guild.id, default=""
        )
        guild_default_hints = store.get_default_name_hints(ctx.guild.id, default="")
        summary_language = (
            str(resolved_campaign.get("summary_language") or "").strip()
            or guild_default_language
        )
        session_context = (
            str(resolved_campaign.get("session_context") or "").strip()
            or guild_default_context
        )
        name_hints = (
            str(resolved_campaign.get("name_hints") or "").strip()
            or guild_default_hints
        )

        chunks: list[str] = []
        for session_dir in sessions:
            summary_file = session_dir / "summary.md"
            transcript_file = session_dir / "full_transcript.txt"
            if summary_file.exists():
                body = summary_file.read_text(encoding="utf-8", errors="ignore")
            elif transcript_file.exists():
                body = transcript_file.read_text(encoding="utf-8", errors="ignore")
            else:
                body = "_No summary/transcript file found._"
            chunks.append(f"## Session {session_dir.name}\n{body.strip()[:20000]}")

        aggregate_input = (
            f"Campaign final summary request.\n"
            f"Campaign: {campaign_name} ({campaign_id})\n"
            f"Sessions included: {len(sessions)}\n\n" + "\n\n".join(chunks)
        )
        started = time.perf_counter()
        try:
            final_summary = await llm.generate_summary(
                aggregate_input,
                language=summary_language,
                session_context=session_context,
                name_hints=name_hints,
            )
        except Exception as exc:
            await ctx.followup.send(
                f"Campaign summarize failed: `{exc}`",
                ephemeral=True,
            )
            return

        out_dir = (
            settings.data_dir
            / "campaigns"
            / str(ctx.guild.id)
            / campaign_id
            / "summaries"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_summary = out_dir / f"campaign_summary_{ts}.md"
        out_input = out_dir / f"campaign_summary_input_{ts}.txt"
        out_summary.write_text(final_summary, encoding="utf-8")
        out_input.write_text(aggregate_input, encoding="utf-8")

        chronicle_channel_id = store.get_chronicle_channel(ctx.guild.id)
        target_channel: discord.abc.Messageable | None = None
        if chronicle_channel_id is not None:
            maybe = ctx.guild.get_channel(chronicle_channel_id)
            if isinstance(maybe, discord.TextChannel):
                target_channel = maybe
        if target_channel is None:
            target_channel = ctx.channel

        await try_send(
            target_channel,
            (
                f"Campaign final summary created for `{campaign_name}`. "
                f"Sessions: `{len(sessions)}`. "
                f"Duration: `{time.perf_counter() - started:.2f}s`."
            ),
        )
        await try_send_file(
            target_channel,
            str(out_summary),
            content="## Campaign Final Summary (.md)",
        )
        await ctx.followup.send(
            f"Campaign summary saved: `{out_summary}`",
            ephemeral=True,
        )

    @bot.slash_command(
        name="chronicle_campaign_summarize",
        description="Generate final summary across all sessions in campaign",
    )
    async def chronicle_campaign_summarize(
        ctx: discord.ApplicationContext,
        campaign: str = discord.Option(
            str,
            description="Campaign id or exact name (empty = active campaign)",
            autocomplete=campaign_autocomplete,
            required=False,
            default="",
        ),
    ) -> None:
        await _run_campaign_summarize(ctx, campaign=campaign, limit=5000)

    @bot.slash_command(
        name="chronicle_campaign_sum_range",
        description="Generate campaign summary with date range and limit filters",
    )
    async def chronicle_campaign_summarize_range(
        ctx: discord.ApplicationContext,
        campaign: str = discord.Option(
            str,
            description="Campaign id or exact name (empty = active campaign)",
            autocomplete=campaign_autocomplete,
            required=False,
            default="",
        ),
        from_date: str = discord.Option(
            str,
            description="Optional from date YYYY-MM-DD",
            required=False,
            default="",
        ),
        to_date: str = discord.Option(
            str,
            description="Optional to date YYYY-MM-DD",
            required=False,
            default="",
        ),
        limit: int = discord.Option(
            int,
            description="Max sessions to include after date filter",
            required=False,
            default=50,
            min_value=1,
            max_value=500,
        ),
    ) -> None:
        await _run_campaign_summarize(
            ctx,
            campaign=campaign,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )

    @bot.slash_command(
        name="chronicle_reconnect",
        description="Force reconnect voice and try to resume recording",
    )
    async def chronicle_reconnect(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        target_channel: VoiceLikeChannel | None = None
        if state.voice_channel_id is not None:
            target_channel = _as_voice_like(
                ctx.guild.get_channel(state.voice_channel_id)
            )
        if target_channel is None:
            configured = store.get_voice_channel(ctx.guild.id)
            if configured is not None:
                target_channel = _as_voice_like(ctx.guild.get_channel(configured))
        if target_channel is None:
            target_channel = await resolve_invoking_voice_channel(ctx)
        if target_channel is None:
            await ctx.followup.send(
                "Could not resolve target voice channel. Use /chronicle_setup_voice_here first.",
                ephemeral=True,
            )
            return

        try:
            if ctx.guild.voice_client is not None:
                await ctx.guild.voice_client.disconnect(force=True)
                await asyncio.sleep(0.5)

            voice_client = await connect_voice_with_retry(
                ctx.guild, target_channel, attempts=3
            )
            if not await wait_voice_ready(voice_client, timeout_s=20.0):
                raise RuntimeError("Voice connection did not become ready in time.")

            resumed = False
            if (
                state.done_callback is not None
                and state.fallback_channel is not None
                and not state.processing
                and not state.finalizing
            ):
                previous_sink = state.sink
                resume_sink = discord.sinks.WaveSink()
                await start_recording_with_retry(
                    voice_client=voice_client,
                    sink=resume_sink,
                    done_cb=state.done_callback,
                    text_channel=state.fallback_channel,
                    guild_id=ctx.guild.id,
                    timeout_s=20.0,
                )
                if previous_sink is not None:
                    await persist_recording_segment(
                        ctx.guild,
                        state,
                        previous_sink,
                        fallback_channel=state.fallback_channel,
                    )
                state.sink = resume_sink
                stop_background_tasks(state)
                state.health_task = asyncio.create_task(
                    monitor_voice_health(
                        ctx.guild.id,
                        target_channel,
                        state.fallback_channel,
                        state.done_callback,
                    )
                )
                state.rotation_task = asyncio.create_task(
                    rotation_loop(
                        ctx.guild.id,
                        state.fallback_channel,
                        state.done_callback,
                    )
                )
                state.voice_channel_id = target_channel.id
                upsert_active_session(
                    ctx.guild.id,
                    status="recording",
                    voice_channel_id=target_channel.id,
                    chronicle_channel_id=store.get_chronicle_channel(ctx.guild.id),
                    segment_count=state.persisted_segments + 1,
                    finalizing=False,
                )
                resumed = True

            if resumed:
                await ctx.followup.send(
                    f"Reconnected to {target_channel.mention} and resumed recording.",
                    ephemeral=True,
                )
            else:
                await ctx.followup.send(
                    f"Reconnected to {target_channel.mention}. Use /chronicle_start to start recording.",
                    ephemeral=True,
                )
        except Exception as exc:
            logger.exception("[reconnect] manual_failed guild_id=%s", ctx.guild.id)
            await ctx.followup.send(f"Manual reconnect failed: `{exc}`", ephemeral=True)

    @bot.slash_command(
        name="chronicle_list_voice", description="List voice/stage channels with IDs"
    )
    async def chronicle_list_voice(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        items: list[str] = []
        for channel in ctx.guild.voice_channels:
            items.append(f"- {channel.name} (`{channel.id}`)")

        if not items:
            await ctx.respond("No voice channels found in this server.", ephemeral=True)
            return

        await ctx.respond("Voice channels:\n" + "\n".join(items), ephemeral=True)

    @bot.slash_command(
        name="chronicle_setup_voice_here",
        description="Use your current voice channel as default",
    )
    async def chronicle_setup_voice_here(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return

        voice_channel = await resolve_invoking_voice_channel(ctx)
        if voice_channel is None:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return
        store.set_voice_channel(ctx.guild.id, voice_channel.id)
        await ctx.respond(
            f"Default voice channel set to {voice_channel.mention}.", ephemeral=True
        )

    @bot.slash_command(
        name="chronicle_start",
        description="Join your voice channel and start recording",
    )
    async def chronicle_start(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.user is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return
        await ctx.defer(ephemeral=True)

        voice_channel: VoiceLikeChannel | None = None
        auto_notes: list[str] = []
        configured_voice_channel_id = store.get_voice_channel(ctx.guild.id)
        if configured_voice_channel_id is not None:
            configured_channel = ctx.guild.get_channel(configured_voice_channel_id)
            voice_like = _as_voice_like(configured_channel)
            if voice_like is not None:
                voice_channel = voice_like
            else:
                logger.warning(
                    "[session] configured_voice_missing guild_id=%s voice_channel_id=%s",
                    ctx.guild.id,
                    configured_voice_channel_id,
                )
                auto_notes.append(
                    "Saved default voice channel was missing, using your current voice channel."
                )

        if voice_channel is None:
            voice_channel = await resolve_invoking_voice_channel(ctx)
        if voice_channel is None:
            await ctx.followup.send("Join a voice channel first.", ephemeral=True)
            return
        if configured_voice_channel_id != voice_channel.id:
            store.set_voice_channel(ctx.guild.id, voice_channel.id)
            if configured_voice_channel_id is None:
                auto_notes.append(
                    "Default voice channel was not set, saved your current voice channel."
                )
        configured_chronicle_channel_id = store.get_chronicle_channel(ctx.guild.id)
        if configured_chronicle_channel_id is None and isinstance(
            ctx.channel, discord.TextChannel
        ):
            store.set_chronicle_channel(ctx.guild.id, ctx.channel.id)
            auto_notes.append(
                "Chronicle text channel was not set, saved this channel as default."
            )
        logger.info(
            "[session] start_requested guild_id=%s requested_by=%s voice_channel_id=%s",
            ctx.guild.id,
            ctx.user.id,
            voice_channel.id,
        )

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send(
                "Recording already running for this guild.", ephemeral=True
            )
            return
        if state.processing:
            await ctx.followup.send(
                "Previous recording is still processing.", ephemeral=True
            )
            return
        active_campaign_id = store.get_active_campaign_id(ctx.guild.id)
        if not active_campaign_id:
            campaign_name = f"Campaign {datetime.now(UTC).strftime('%Y-%m-%d')}"
            created_campaign: dict[str, Any] | None = None
            try:
                created_campaign = store.create_campaign(
                    ctx.guild.id,
                    name=campaign_name,
                    summary_language=store.get_default_summary_language(
                        ctx.guild.id, default="ru"
                    ),
                )
            except ValueError:
                # Name collision: create a unique fallback name.
                created_campaign = store.create_campaign(
                    ctx.guild.id,
                    name=f"{campaign_name} {datetime.now(UTC).strftime('%H%M%S')}",
                    summary_language=store.get_default_summary_language(
                        ctx.guild.id, default="ru"
                    ),
                )
            auto_notes.append(
                f"Created and selected campaign `{created_campaign.get('name', '')}` automatically."
            )
        resolved_campaign = store.resolve_active_campaign_settings(ctx.guild.id)
        state.started_at_utc = datetime.now(UTC)
        state.finalizing = False
        state.segment_sinks = []
        state.session_id = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        state.session_dir = None
        state.persisted_segments = 0
        state.restart_in_progress = False
        state.rotation_triggered = 0
        state.rotation_resumed = 0
        state.rotation_failed = 0
        state.reconnect_attempts = 0
        state.reconnect_successes = 0
        state.reconnect_failures = 0
        state.decode_burst_triggers = 0
        state.decode_burst_skipped_cooldown = 0
        state.last_decode_burst_at_utc = None
        state.decode_recovery_cooldown_until = 0.0
        state.campaign_id = resolved_campaign.get("campaign_id", "")
        state.campaign_name = resolved_campaign.get("campaign_name", "")
        state.summary_language = resolved_campaign.get("summary_language", "ru")
        state.session_context = resolved_campaign.get("session_context", "")
        state.name_hints = resolved_campaign.get("name_hints", "")
        decode_error_events.clear()
        stop_background_tasks(state)
        ensure_session_dir(state, ctx.guild.id)
        write_session_checkpoint(state, ctx.guild.id, status="recording")
        upsert_active_session(
            ctx.guild.id,
            status="starting",
            voice_channel_id=voice_channel.id,
            chronicle_channel_id=store.get_chronicle_channel(ctx.guild.id),
            segment_count=0,
            finalizing=False,
        )

        async def on_finished(
            finished_sink: discord.sinks.Sink,
            fallback_channel: discord.abc.Messageable,
            guild_id: int,
        ) -> None:
            logger.info(
                "[on_finished] called guild=%s tracks=%s",
                guild_id,
                len(finished_sink.audio_data),
            )
            state = guild_state.setdefault(guild_id, GuildRecordingState())
            state.sink = None
            if state.processing:
                logger.info(
                    "[on_finished] duplicate_event_ignored guild=%s reason=already_processing",
                    guild_id,
                )
                return

            guild = bot.get_guild(guild_id)
            if guild is None:
                return

            chronicle_channel_id = store.get_chronicle_channel(guild_id)
            target_channel: discord.abc.Messageable | None = None
            if chronicle_channel_id is not None:
                maybe = guild.get_channel(chronicle_channel_id)
                if isinstance(maybe, discord.TextChannel):
                    target_channel = maybe
            if target_channel is None:
                target_channel = fallback_channel

            await persist_recording_segment(
                guild,
                state,
                finished_sink,
                fallback_channel=target_channel,
            )

            # During finalization, Discord can emit extra on_finished callbacks from
            # voice reconnect/teardown races. Do not start a second processing pass.
            if state.finalizing and state.processing:
                logger.info(
                    "[on_finished] duplicate_finalizing_event_ignored guild=%s tracks=%s",
                    guild_id,
                    len(finished_sink.audio_data),
                )
                return

            # Rotation stop: restart next segment instead of processing final output.
            if not state.finalizing:
                if state.restart_in_progress:
                    logger.info(
                        "[rotation] restart_skipped guild_id=%s reason=in_progress",
                        guild_id,
                    )
                    return
                state.restart_in_progress = True
                try:
                    target_voice = (
                        guild.get_channel(state.voice_channel_id)
                        if state.voice_channel_id
                        else None
                    )
                    target_voice = _as_voice_like(target_voice)
                    if target_voice is None:
                        state.rotation_failed += 1
                        await try_send(
                            target_channel,
                            "Rotation failed: voice channel is no longer available.",
                        )
                        return
                    recovered_client = await connect_voice_with_retry(
                        guild, target_voice, attempts=3
                    )
                    next_sink = discord.sinks.WaveSink()
                    state.sink = next_sink
                    await asyncio.sleep(1.0)
                    await start_recording_with_retry(
                        voice_client=recovered_client,
                        sink=next_sink,
                        done_cb=on_finished,
                        text_channel=fallback_channel,
                        guild_id=guild_id,
                        timeout_s=30.0,
                    )
                    upsert_active_session(
                        guild_id,
                        status="recording",
                        voice_channel_id=target_voice.id,
                        chronicle_channel_id=store.get_chronicle_channel(guild_id),
                        segment_count=state.persisted_segments + 1,
                        finalizing=False,
                    )
                    state.rotation_resumed += 1
                    await try_send(
                        target_channel, "Recording segment rotated and resumed."
                    )
                except Exception as exc:
                    state.rotation_failed += 1
                    state.sink = None
                    clear_active_session(guild_id)
                    await try_send(
                        target_channel,
                        f"Voice reconnect failed, but segment was saved to disk. `{exc}` Use `/chronicle_start` to continue.",
                    )
                finally:
                    state.restart_in_progress = False
                return

            state.processing = True
            processing_started = time.perf_counter()
            try:
                if state.persisted_segments <= 0 or state.session_dir is None:
                    sent_no_audio = await try_send(
                        target_channel,
                        "Recording finished, but no audio data was captured.",
                    )
                    if (not sent_no_audio) and not same_messageable(
                        target_channel, fallback_channel
                    ):
                        await try_send(
                            fallback_channel,
                            "Recording finished, but no audio data was captured.",
                        )
                    logger.warning("[on_finished] no audio captured guild=%s", guild_id)
                    return

                sent = await try_send(
                    target_channel,
                    "Processing recording: Whisper transcription + local LLM summary...",
                )
                if (not sent) and not same_messageable(
                    target_channel, fallback_channel
                ):
                    await try_send(
                        fallback_channel,
                        "Processing recording: Whisper transcription + local LLM summary...",
                    )
                logger.info(
                    "[session] processing_begin guild_id=%s segments=%s campaign_id=%s language=%s timeout_s=%s",
                    guild_id,
                    state.persisted_segments,
                    state.campaign_id,
                    state.summary_language,
                    settings.processing_timeout_seconds,
                )
                artifacts = await asyncio.wait_for(
                    processor.reprocess_saved_session(
                        session_dir=state.session_dir,
                        summary_language=state.summary_language,
                        session_context=state.session_context,
                        name_hints=state.name_hints,
                        campaign_id=state.campaign_id,
                        campaign_name=state.campaign_name,
                    ),
                    timeout=settings.processing_timeout_seconds,
                )

                posted = await try_send(
                    target_channel, f"Session saved: `{artifacts.session_dir}`"
                )
                if (not posted) and not same_messageable(
                    target_channel, fallback_channel
                ):
                    target_channel = fallback_channel
                    await try_send(
                        target_channel, f"Session saved: `{artifacts.session_dir}`"
                    )
                transcript_sent = await try_send_file(
                    target_channel,
                    str(artifacts.full_transcript_txt_path),
                    content="## Full Transcript (attached as .txt)",
                )
                if (not transcript_sent) and not same_messageable(
                    target_channel, fallback_channel
                ):
                    await try_send_file(
                        fallback_channel,
                        str(artifacts.full_transcript_txt_path),
                        content="## Full Transcript (attached as .txt)",
                    )

                uploaded_audio_count = 0
                if artifacts.mixed_audio_path and artifacts.mixed_audio_path.exists():
                    mixed_sent = await try_send_file(
                        target_channel,
                        str(artifacts.mixed_audio_path),
                        content="## Mixed Session Audio (.mp3)",
                    )
                    if (not mixed_sent) and not same_messageable(
                        target_channel, fallback_channel
                    ):
                        mixed_sent = await try_send_file(
                            fallback_channel,
                            str(artifacts.mixed_audio_path),
                            content="## Mixed Session Audio (.mp3)",
                        )
                    uploaded_audio_count = 1 if mixed_sent else 0
                elif settings.publish_per_speaker_audio:
                    mp3_paths = [
                        str(item.audio_path)
                        for item in artifacts.speaker_transcripts
                        if item.audio_path.suffix.lower() == ".mp3"
                        and item.audio_path.exists()
                    ]
                    if mp3_paths:
                        sent_count = await try_send_files(
                            target_channel,
                            mp3_paths,
                            content="## Audio Tracks (.mp3)",
                        )
                        if (sent_count == 0) and not same_messageable(
                            target_channel, fallback_channel
                        ):
                            sent_count = await try_send_files(
                                fallback_channel,
                                mp3_paths,
                                content="## Audio Tracks (.mp3)",
                            )
                        uploaded_audio_count = sent_count
                        if sent_count < len(mp3_paths):
                            await try_send(
                                target_channel,
                                f"Uploaded {sent_count}/{len(mp3_paths)} mp3 files. "
                                f"Remaining files are still saved in `{artifacts.session_dir}`.",
                            )
                logger.info(
                    "[session] processing_done guild_id=%s session_dir=%s transcript_file=%s mp3_files=%s duration_s=%.3f",
                    guild_id,
                    artifacts.session_dir,
                    artifacts.full_transcript_txt_path.name,
                    uploaded_audio_count,
                    time.perf_counter() - processing_started,
                )
                await try_send(target_channel, "## AI Session Summary")
                try:
                    await send_long(target_channel, artifacts.summary_markdown)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    if not same_messageable(target_channel, fallback_channel):
                        await send_long(fallback_channel, artifacts.summary_markdown)
                quality_report = await build_quality_report(
                    state, artifacts.speaker_transcripts
                )
                await try_send(target_channel, quality_report)
            except TimeoutError:
                logger.warning(
                    "[session] processing_timeout guild_id=%s timeout_s=%s",
                    guild_id,
                    settings.processing_timeout_seconds,
                )
                await try_send(
                    fallback_channel,
                    "Processing timed out. Check Whisper/LLM availability and bot logs.",
                )
            except Exception as exc:
                sent = await try_send(
                    fallback_channel, f"Error while processing recording: `{exc}`"
                )
                if not sent:
                    logger.exception(
                        "[on_finished] processing error guild=%s", guild_id
                    )
            finally:
                state.restart_in_progress = False
                state.processing = False
                state.finalizing = False
                state.voice_channel_id = None
                state.started_at_utc = None
                state.segment_sinks = []
                state.session_id = ""
                state.session_dir = None
                state.persisted_segments = 0
                state.decode_recovery_cooldown_until = 0.0
                state.campaign_id = ""
                state.campaign_name = ""
                state.summary_language = "ru"
                state.session_context = ""
                state.name_hints = ""
                stop_background_tasks(state)
                clear_active_session(guild_id)
                guild = bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    await guild.voice_client.disconnect(force=False)

        try:
            voice_client = await connect_voice_with_retry(ctx.guild, voice_channel)

            sink = discord.sinks.WaveSink()
            state.sink = sink
            # Keep a short settling delay for Discord voice handshake.
            await asyncio.sleep(2.0)
            await start_recording_with_retry(
                voice_client=voice_client,
                sink=sink,
                done_cb=on_finished,
                text_channel=ctx.channel,
                guild_id=ctx.guild.id,
            )
            state.voice_channel_id = voice_channel.id
            state.done_callback = on_finished
            state.fallback_channel = ctx.channel
            state.health_task = asyncio.create_task(
                monitor_voice_health(
                    ctx.guild.id, voice_channel, ctx.channel, on_finished
                )
            )
            state.rotation_task = asyncio.create_task(
                rotation_loop(ctx.guild.id, ctx.channel, on_finished)
            )
            upsert_active_session(
                ctx.guild.id,
                status="recording",
                voice_channel_id=voice_channel.id,
                chronicle_channel_id=store.get_chronicle_channel(ctx.guild.id),
                segment_count=state.persisted_segments + 1,
                finalizing=False,
            )
        except (RecordingException, RuntimeError) as exc:
            state.sink = None
            state.voice_channel_id = None
            state.finalizing = False
            state.started_at_utc = None
            state.session_id = ""
            state.session_dir = None
            state.persisted_segments = 0
            state.restart_in_progress = False
            state.done_callback = None
            state.fallback_channel = None
            stop_background_tasks(state)
            clear_active_session(ctx.guild.id)
            snapshot = voice_state_snapshot(ctx.guild.voice_client)
            if ctx.guild.voice_client:
                await ctx.guild.voice_client.disconnect(force=True)
            await ctx.followup.send(
                f"Could not start recording: `{exc}`\nVoice state: `{snapshot}`",
                ephemeral=True,
            )
            return
        except Exception as exc:
            state.sink = None
            state.voice_channel_id = None
            state.finalizing = False
            state.started_at_utc = None
            state.session_id = ""
            state.session_dir = None
            state.persisted_segments = 0
            state.restart_in_progress = False
            state.done_callback = None
            state.fallback_channel = None
            stop_background_tasks(state)
            clear_active_session(ctx.guild.id)
            snapshot = voice_state_snapshot(ctx.guild.voice_client)
            if ctx.guild.voice_client:
                await ctx.guild.voice_client.disconnect(force=True)
            await ctx.followup.send(
                f"Unexpected error while starting recording: `{exc}`\nVoice state: `{snapshot}`",
                ephemeral=True,
            )
            return

        start_message = f"Recording started in {voice_channel.mention}."
        if auto_notes:
            start_message += "\n" + "\n".join(f"- {note}" for note in auto_notes)
        await ctx.followup.send(start_message, ephemeral=True)

    @bot.slash_command(
        name="chronicle_stop", description="Stop recording and build chronicle"
    )
    async def chronicle_stop(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                "This command can be used only in a server.", ephemeral=True
            )
            return

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        voice_client = ctx.guild.voice_client
        if voice_client is None or state.sink is None:
            await ctx.respond("No active recording.", ephemeral=True)
            return
        state.finalizing = True
        stop_background_tasks(state)
        upsert_active_session(
            ctx.guild.id,
            status="finalizing",
            voice_channel_id=state.voice_channel_id,
            chronicle_channel_id=store.get_chronicle_channel(ctx.guild.id),
            segment_count=state.persisted_segments,
            finalizing=True,
        )
        write_session_checkpoint(state, ctx.guild.id, status="finalizing")
        voice_client.stop_recording()
        await ctx.respond("Recording stopped. Processing started.", ephemeral=True)

    @bot.slash_command(
        name="chronicle_leave", description="Disconnect bot from voice channel"
    )
    async def chronicle_leave(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.guild.voice_client is None:
            await ctx.respond("Bot is not in a voice channel.", ephemeral=True)
            return
        await ctx.guild.voice_client.disconnect(force=False)
        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        state.sink = None
        state.finalizing = False
        state.voice_channel_id = None
        state.started_at_utc = None
        state.session_id = ""
        state.session_dir = None
        state.persisted_segments = 0
        state.restart_in_progress = False
        state.done_callback = None
        state.fallback_channel = None
        stop_background_tasks(state)
        clear_active_session(ctx.guild.id)
        await ctx.respond("Disconnected from voice channel.", ephemeral=True)

    return bot


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    bot = build_bot(settings)
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
