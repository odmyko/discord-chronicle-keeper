import asyncio
from dataclasses import dataclass
from datetime import datetime, UTC, timedelta
import json
import logging
from pathlib import Path
import re
import shutil
import struct
import time
from typing import Iterable

import discord
from discord.ext import commands
from discord.sinks.errors import RecordingException
import discord.gateway as discord_gateway

from .config import Settings, load_settings
from .llm_client import LLMClient
from .processor import SessionProcessor
from .storage import GuildSettingsStore
from .whisper_client import WhisperClient


DISCORD_SAFE_LIMIT = 1900
VoiceLikeChannel = discord.VoiceChannel
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
        supported_modes = list(getattr(discord.VoiceClient, "supported_modes", ()) or ())
        for mode in extra_voice_modes:
            if mode not in supported_modes:
                supported_modes.append(mode)
        discord.VoiceClient.supported_modes = tuple(supported_modes)
    except Exception:
        pass

    # Prefer xchacha mode when available. Some py-cord builds can connect with AES mode
    # but produce decode errors on receive in certain environments.
    try:
        if not getattr(discord_gateway.DiscordVoiceWebSocket, "_chronicle_mode_patch", False):
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

                modes = [mode for mode in data["modes"] if mode in self._connection.supported_modes]
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
                discord_gateway._log.info("selected the voice protocol for use (%s)", mode)

            discord_gateway.DiscordVoiceWebSocket.initial_connection = _patched_initial_connection
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

            setattr(discord.VoiceClient, "_encrypt_aead_aes256_gcm_rtpsize", _encrypt_aead_aes256_gcm_rtpsize)

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

            setattr(discord.VoiceClient, "_decrypt_aead_aes256_gcm_rtpsize", _decrypt_aead_aes256_gcm_rtpsize)
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
    processor = SessionProcessor(
        settings.data_dir,
        whisper,
        llm,
        audio_normalize=settings.audio_normalize,
        audio_target_sample_rate=settings.audio_target_sample_rate,
        audio_target_channels=settings.audio_target_channels,
        audio_mp3_vbr_quality=settings.audio_mp3_vbr_quality,
        summary_chunk_chars=settings.summary_chunk_chars,
    )
    guild_state: dict[int, GuildRecordingState] = {}

    async def send_long(channel: discord.abc.Messageable, text: str) -> None:
        for chunk in chunk_text(text):
            await channel.send(chunk)

    async def try_send(channel: discord.abc.Messageable | None, text: str) -> bool:
        if channel is None:
            return False
        try:
            await channel.send(text)
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False

    async def try_send_file(
        channel: discord.abc.Messageable | None,
        path: str,
        content: str | None = None,
    ) -> bool:
        if channel is None:
            return False
        try:
            await channel.send(content=content, file=discord.File(path))
            return True
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            return False

    async def try_send_files(
        channel: discord.abc.Messageable | None,
        paths: list[str],
        content: str | None = None,
        batch_size: int = 5,
    ) -> int:
        if channel is None or not paths:
            return 0
        sent = 0
        for i in range(0, len(paths), batch_size):
            files = [discord.File(p) for p in paths[i : i + batch_size]]
            try:
                await channel.send(content=content if i == 0 else None, files=files)
                sent += len(files)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                continue
        return sent

    async def ffprobe_audio(path: str) -> tuple[float, int | None, int | None, int | None]:
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
        lines = [line.strip() for line in out.decode("utf-8", errors="ignore").splitlines() if line.strip()]
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
            duration, bit_rate, sample_rate, channels = await ffprobe_audio(str(item.audio_path))
            if duration > 0:
                durations.append(duration)
            if bit_rate:
                bitrates.append(bit_rate)
            if sample_rate:
                sample_rates.append(sample_rate)
            if channels:
                channels_list.append(channels)

        total_duration = sum(durations)
        avg_bitrate_kbps = (sum(bitrates) / len(bitrates) / 1000.0) if bitrates else None
        dominant_sample_rate = max(sample_rates, key=sample_rates.count) if sample_rates else None
        dominant_channels = max(channels_list, key=channels_list.count) if channels_list else None
        elapsed_s = (
            (datetime.now(UTC) - state.started_at_utc).total_seconds()
            if state.started_at_utc is not None
            else None
        )

        lines = ["## Recording Quality Report"]
        lines.append(f"- Speaker tracks: `{len(speaker_items)}`")
        lines.append(f"- Rotation events: `{state.rotation_triggered}` (resumed `{state.rotation_resumed}`, failed `{state.rotation_failed}`)")
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
        if "active_sessions" not in payload or not isinstance(payload.get("active_sessions"), dict):
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
            "voice_channel_id": voice_channel_id if voice_channel_id is not None else current.get("voice_channel_id"),
            "chronicle_channel_id": (
                chronicle_channel_id if chronicle_channel_id is not None else current.get("chronicle_channel_id")
            ),
            "segment_count": segment_count if segment_count is not None else current.get("segment_count", 0),
            "finalizing": finalizing if finalizing is not None else current.get("finalizing", False),
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
            p for p in guild_sessions_dir.iterdir() if p.is_dir() and session_timestamp_utc(p.name) is not None
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.name, reverse=True)
        return candidates[0]

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
            logger.info("[recovery] startup_skip recovery_auto_post_partial=%s", settings.recovery_auto_post_partial)
            return

        sessions_root = settings.data_dir / "sessions"
        if not sessions_root.exists():
            logger.info("[recovery] startup_skip reason=no_sessions_root path=%s", sessions_root)
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

    async def wait_voice_ready(voice_client: discord.VoiceClient, timeout_s: float = 20.0) -> bool:
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
        done_cb,
        text_channel: discord.abc.GuildChannel,
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
                    if (not current.is_connected()) or current.channel is None or current.channel.id != voice_channel.id:
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
        runtime_state = load_runtime_state()
        active_count = len(runtime_state.get("active_sessions", {}))
        if active_count > 0:
            logger.info("[runtime] detected %s active session entries from previous run", active_count)
        await run_startup_cleanup()
        await recover_unfinished_sessions()

    async def resolve_invoking_member(ctx: discord.ApplicationContext) -> discord.Member | None:
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

    async def resolve_invoking_voice_channel(ctx: discord.ApplicationContext) -> VoiceLikeChannel | None:
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

    def resolve_text_channel(ctx: discord.ApplicationContext, raw_channel: object | None) -> discord.TextChannel | None:
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
                    ch for ch in ctx.guild.text_channels if ch.name.casefold() == channel_value.casefold()
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

    def resolve_voice_channel(ctx: discord.ApplicationContext, raw_channel: object | None) -> VoiceLikeChannel | None:
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
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return False
        if isinstance(ctx.author, discord.Member):
            perms = ctx.author.guild_permissions
            if perms.administrator or perms.manage_guild:
                return True
        await ctx.respond("You need `Manage Server` permission to run this command.", ephemeral=True)
        return False

    @bot.slash_command(name="chronicle_cleanup_now", description="Delete old session artifacts by retention policy")
    async def chronicle_cleanup_now(ctx: discord.ApplicationContext) -> None:
        if not await require_manage_guild(ctx):
            return
        if not settings.auto_cleanup_enabled:
            await ctx.respond("Cleanup is disabled (`AUTO_CLEANUP_ENABLED=false`).", ephemeral=True)
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

    @bot.slash_command(name="chronicle_purge_session", description="Delete one saved session by ID")
    async def chronicle_purge_session(
        ctx: discord.ApplicationContext,
        session_id: str = discord.Option(
            str,
            description="Session folder id, e.g. 20260219_201349",
            required=True,
        ),
    ) -> None:
        if not await require_manage_guild(ctx):
            return
        if not settings.allow_purge_commands:
            await ctx.respond("Purge commands are disabled (`ALLOW_PURGE_COMMANDS=false`).", ephemeral=True)
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
            await ctx.respond(f"Failed to delete `{session_id}`: `{exc}`", ephemeral=True)
            return
        await ctx.respond(f"Session `{session_id}` deleted.", ephemeral=True)

    @bot.slash_command(name="chronicle_purge_guild_data", description="Delete all saved data for this guild")
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
            await ctx.respond("Purge commands are disabled (`ALLOW_PURGE_COMMANDS=false`).", ephemeral=True)
            return
        if ctx.guild is None:
            return
        if confirm.strip() != "PURGE":
            await ctx.respond("Confirmation failed. Type exactly `PURGE`.", ephemeral=True)
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
        await ctx.respond("All saved guild session data has been deleted.", ephemeral=True)

    @bot.slash_command(name="chronicle_setup", description="Set text channel for chronicle reports")
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
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        resolved_channel = resolve_text_channel(ctx, channel)

        if resolved_channel is None:
            await ctx.respond(
                "Could not resolve a text channel. Use this command in the target text channel or pass #channel.",
                ephemeral=True,
            )
            return

        store.set_chronicle_channel(ctx.guild.id, resolved_channel.id)
        await ctx.respond(f"Chronicle channel set to {resolved_channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_setup_here", description="Set current text channel for chronicle reports")
    async def chronicle_setup_here(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.channel, discord.TextChannel):
            await ctx.respond("Run this command from a server text channel.", ephemeral=True)
            return
        store.set_chronicle_channel(ctx.guild.id, ctx.channel.id)
        await ctx.respond(f"Chronicle channel set to {ctx.channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_setup_voice", description="Set default voice channel for recording")
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
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        resolved_channel = resolve_voice_channel(ctx, channel)

        if resolved_channel is None:
            await ctx.respond(
                "Could not resolve selected voice channel.",
                ephemeral=True,
            )
            return

        store.set_voice_channel(ctx.guild.id, resolved_channel.id)
        await ctx.respond(f"Default voice channel set to {resolved_channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_setup_channels", description="Set both voice and transcript text channels")
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
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        resolved_voice = resolve_voice_channel(ctx, voice_channel)
        resolved_text = resolve_text_channel(ctx, transcript_channel)
        if resolved_voice is None or resolved_text is None:
            await ctx.respond("Could not resolve one or both channels from the selected values.", ephemeral=True)
            return

        store.set_voice_channel(ctx.guild.id, resolved_voice.id)
        store.set_chronicle_channel(ctx.guild.id, resolved_text.id)
        await ctx.respond(
            f"Setup complete.\nVoice: {resolved_voice.mention}\nTranscript: {resolved_text.mention}",
            ephemeral=True,
        )

    @bot.slash_command(name="chronicle_setup_language", description="Set language for generated session summary")
    async def chronicle_setup_language(
        ctx: discord.ApplicationContext,
        language: str = discord.Option(
            str,
            description="Summary language",
            choices=["en", "uk", "ru"],
            required=True,
        ),
    ) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return
        store.set_summary_language(ctx.guild.id, language)
        await ctx.respond(f"Summary language set to `{language}`.", ephemeral=True)

    @bot.slash_command(name="chronicle_status", description="Show recorder status and health counters")
    async def chronicle_status(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        configured_voice_id = store.get_voice_channel(ctx.guild.id)
        configured_voice = ctx.guild.get_channel(configured_voice_id) if configured_voice_id else None
        configured_text_id = store.get_chronicle_channel(ctx.guild.id)
        configured_text = ctx.guild.get_channel(configured_text_id) if configured_text_id else None
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
        await ctx.respond("\n".join(lines), ephemeral=True)

    @bot.slash_command(
        name="chronicle_reprocess_last",
        description="Reprocess the latest saved session for this guild",
    )
    async def chronicle_reprocess_last(ctx: discord.ApplicationContext) -> None:
        if not await require_manage_guild(ctx):
            return
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send("Cannot reprocess while recording is active.", ephemeral=True)
            return
        if state.processing:
            await ctx.followup.send("Another processing task is already running.", ephemeral=True)
            return

        session_dir = latest_session_dir_for_guild(ctx.guild.id)
        if session_dir is None:
            await ctx.followup.send("No saved sessions found for this guild.", ephemeral=True)
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
        summary_language = store.get_summary_language(ctx.guild.id, default="ru")
        await try_send(
            target_channel,
            f"Reprocessing latest saved session: `{session_dir.name}` (language `{summary_language}`)...",
        )
        try:
            artifacts = await asyncio.wait_for(
                processor.reprocess_saved_session(
                    session_dir=session_dir,
                    summary_language=summary_language,
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
            logger.info(
                "[reprocess] command_done guild_id=%s session_dir=%s duration_s=%.3f",
                ctx.guild.id,
                session_dir,
                time.perf_counter() - started,
            )
            await ctx.followup.send(f"Reprocessed `{session_dir.name}` successfully.", ephemeral=True)
        except TimeoutError:
            await try_send(target_channel, "Reprocess timed out. Check Whisper/LLM availability.")
            await ctx.followup.send("Reprocess timed out.", ephemeral=True)
        except Exception as exc:
            logger.exception("[reprocess] command_failed guild_id=%s session_dir=%s", ctx.guild.id, session_dir)
            await try_send(target_channel, f"Reprocess failed: `{exc}`")
            await ctx.followup.send(f"Reprocess failed: `{exc}`", ephemeral=True)
        finally:
            state.processing = False

    @bot.slash_command(name="chronicle_list_voice", description="List voice/stage channels with IDs")
    async def chronicle_list_voice(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        items: list[str] = []
        for channel in ctx.guild.voice_channels:
            items.append(f"- {channel.name} (`{channel.id}`)")

        if not items:
            await ctx.respond("No voice channels found in this server.", ephemeral=True)
            return

        await ctx.respond("Voice channels:\n" + "\n".join(items), ephemeral=True)

    @bot.slash_command(name="chronicle_setup_voice_here", description="Use your current voice channel as default")
    async def chronicle_setup_voice_here(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return

        voice_channel = await resolve_invoking_voice_channel(ctx)
        if voice_channel is None:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return
        store.set_voice_channel(ctx.guild.id, voice_channel.id)
        await ctx.respond(f"Default voice channel set to {voice_channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_start", description="Join your voice channel and start recording")
    async def chronicle_start(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.user is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)

        voice_channel: VoiceLikeChannel | None = None
        configured_voice_channel_id = store.get_voice_channel(ctx.guild.id)
        if configured_voice_channel_id is not None:
            configured_channel = ctx.guild.get_channel(configured_voice_channel_id)
            voice_like = _as_voice_like(configured_channel)
            if voice_like is not None:
                voice_channel = voice_like
            else:
                await ctx.followup.send(
                    "Configured default voice channel was not found. Re-run /chronicle_setup_voice_here.",
                    ephemeral=True,
                )
                return

        if voice_channel is None:
            voice_channel = await resolve_invoking_voice_channel(ctx)
        if voice_channel is None:
            await ctx.followup.send("Join a voice channel first.", ephemeral=True)
            return
        logger.info(
            "[session] start_requested guild_id=%s requested_by=%s voice_channel_id=%s",
            ctx.guild.id,
            ctx.user.id,
            voice_channel.id,
        )

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send("Recording already running for this guild.", ephemeral=True)
            return
        if state.processing:
            await ctx.followup.send("Previous recording is still processing.", ephemeral=True)
            return
        state.started_at_utc = datetime.now(UTC)
        state.finalizing = False
        state.segment_sinks = []
        state.rotation_triggered = 0
        state.rotation_resumed = 0
        state.rotation_failed = 0
        state.reconnect_attempts = 0
        state.reconnect_successes = 0
        state.reconnect_failures = 0
        stop_background_tasks(state)
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
            logger.info("[on_finished] called guild=%s tracks=%s", guild_id, len(finished_sink.audio_data))
            state = guild_state.setdefault(guild_id, GuildRecordingState())
            state.sink = None
            if finished_sink.audio_data:
                if state.segment_sinks is None:
                    state.segment_sinks = []
                state.segment_sinks.append(finished_sink)

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

            # Rotation stop: restart next segment instead of processing final output.
            if not state.finalizing:
                try:
                    target_voice = guild.get_channel(state.voice_channel_id) if state.voice_channel_id else None
                    target_voice = _as_voice_like(target_voice)
                    if target_voice is None:
                        state.rotation_failed += 1
                        await try_send(target_channel, "Rotation failed: voice channel is no longer available.")
                        return
                    recovered_client = await connect_voice_with_retry(guild, target_voice, attempts=3)
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
                        segment_count=(len(state.segment_sinks) + 1) if state.segment_sinks else 1,
                        finalizing=False,
                    )
                    state.rotation_resumed += 1
                    await try_send(target_channel, "Recording segment rotated and resumed.")
                except Exception as exc:
                    state.rotation_failed += 1
                    await try_send(
                        target_channel,
                        f"Rotation restart failed: `{exc}`. Use `/chronicle_start` to continue.",
                    )
                return

            state.processing = True
            processing_started = time.perf_counter()
            try:
                if not state.segment_sinks:
                    sent_no_audio = await try_send(
                        target_channel, "Recording finished, but no audio data was captured."
                    )
                    if not sent_no_audio and target_channel is not fallback_channel:
                        await try_send(
                            fallback_channel, "Recording finished, but no audio data was captured."
                        )
                    logger.warning("[on_finished] no audio captured guild=%s", guild_id)
                    return

                sent = await try_send(target_channel, "Processing recording: Whisper transcription + local LLM summary...")
                if not sent and target_channel is not fallback_channel:
                    await try_send(fallback_channel, "Processing recording: Whisper transcription + local LLM summary...")
                summary_language = store.get_summary_language(guild_id, default="ru")
                logger.info(
                    "[session] processing_begin guild_id=%s segments=%s language=%s timeout_s=%s",
                    guild_id,
                    len(state.segment_sinks or []),
                    summary_language,
                    settings.processing_timeout_seconds,
                )
                artifacts = await asyncio.wait_for(
                    processor.process_sinks(guild, state.segment_sinks, summary_language=summary_language),
                    timeout=settings.processing_timeout_seconds,
                )

                posted = await try_send(target_channel, f"Session saved: `{artifacts.session_dir}`")
                if not posted and target_channel is not fallback_channel:
                    target_channel = fallback_channel
                    await try_send(target_channel, f"Session saved: `{artifacts.session_dir}`")
                transcript_sent = await try_send_file(
                    target_channel,
                    str(artifacts.full_transcript_txt_path),
                    content="## Full Transcript (attached as .txt)",
                )
                if (not transcript_sent) and target_channel is not fallback_channel:
                    await try_send_file(
                        fallback_channel,
                        str(artifacts.full_transcript_txt_path),
                        content="## Full Transcript (attached as .txt)",
                    )

                mp3_paths = [
                    str(item.audio_path)
                    for item in artifacts.speaker_transcripts
                    if item.audio_path.suffix.lower() == ".mp3" and item.audio_path.exists()
                ]
                if mp3_paths:
                    sent_count = await try_send_files(
                        target_channel,
                        mp3_paths,
                        content="## Audio Tracks (.mp3)",
                    )
                    if sent_count == 0 and target_channel is not fallback_channel:
                        sent_count = await try_send_files(
                            fallback_channel,
                            mp3_paths,
                            content="## Audio Tracks (.mp3)",
                        )
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
                    len(mp3_paths),
                    time.perf_counter() - processing_started,
                )
                await try_send(target_channel, "## AI Session Summary")
                try:
                    await send_long(target_channel, artifacts.summary_markdown)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    if target_channel is not fallback_channel:
                        await send_long(fallback_channel, artifacts.summary_markdown)
                quality_report = await build_quality_report(state, artifacts.speaker_transcripts)
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
                sent = await try_send(fallback_channel, f"Error while processing recording: `{exc}`")
                if not sent:
                    logger.exception("[on_finished] processing error guild=%s", guild_id)
            finally:
                state.processing = False
                state.finalizing = False
                state.voice_channel_id = None
                state.started_at_utc = None
                state.segment_sinks = []
                stop_background_tasks(state)
                clear_active_session(guild_id)
                guild = bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    await guild.voice_client.disconnect(force=False)

        async def rotation_loop(
            guild_id: int,
            fallback_channel: discord.abc.Messageable,
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
                    logger.warning("[rotation] skip guild_id=%s reason=voice_client_missing", guild_id)
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
                    segment_count=len(state.segment_sinks or []),
                    finalizing=False,
                )
                try:
                    state.rotation_triggered += 1
                    guild.voice_client.stop_recording()
                except Exception as exc:
                    state.rotation_failed += 1
                    logger.exception("[rotation] stop_recording_failed guild_id=%s", guild_id)
                    await try_send(
                        fallback_channel,
                        f"Rotation trigger failed: `{exc}`",
                    )

        async def monitor_voice_health(
            guild_id: int,
            target_voice_channel: VoiceLikeChannel,
            fallback_channel: discord.abc.Messageable,
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
                    logger.warning("[voice-health] monitor_stopped guild_id=%s reason=guild_missing", guild_id)
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
                try:
                    state.reconnect_attempts += 1
                    recovered_client = await connect_voice_with_retry(
                        guild, target_voice_channel, attempts=3
                    )
                    await asyncio.sleep(1.5)
                    await start_recording_with_retry(
                        voice_client=recovered_client,
                        sink=state.sink,
                        done_cb=on_finished,
                        text_channel=fallback_channel,
                        guild_id=guild_id,
                        timeout_s=20.0,
                    )
                    await try_send(
                        fallback_channel,
                        "Voice connection recovered. Recording resumed.",
                    )
                    state.reconnect_successes += 1
                    logger.info("[voice-health] recovered guild_id=%s", guild_id)
                except Exception as exc:
                    state.reconnect_failures += 1
                    logger.exception("[voice-health] reconnect_failed guild_id=%s", guild_id)
                    await try_send(
                        fallback_channel,
                        f"Reconnect attempt failed: `{exc}`. Will retry automatically.",
                    )

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
            state.health_task = asyncio.create_task(
                monitor_voice_health(ctx.guild.id, voice_channel, ctx.channel)
            )
            state.rotation_task = asyncio.create_task(
                rotation_loop(ctx.guild.id, ctx.channel)
            )
            upsert_active_session(
                ctx.guild.id,
                status="recording",
                voice_channel_id=voice_channel.id,
                chronicle_channel_id=store.get_chronicle_channel(ctx.guild.id),
                segment_count=1,
                finalizing=False,
            )
        except (RecordingException, RuntimeError) as exc:
            state.sink = None
            state.voice_channel_id = None
            state.finalizing = False
            state.started_at_utc = None
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

        await ctx.followup.send(f"Recording started in {voice_channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_stop", description="Stop recording and build chronicle")
    async def chronicle_stop(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
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
            segment_count=len(state.segment_sinks or []),
            finalizing=True,
        )
        voice_client.stop_recording()
        await ctx.respond("Recording stopped. Processing started.", ephemeral=True)

    @bot.slash_command(name="chronicle_leave", description="Disconnect bot from voice channel")
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
