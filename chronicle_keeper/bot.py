import asyncio
from dataclasses import dataclass
import re
import struct
from typing import Iterable

import discord
from discord.ext import commands
from discord.sinks.errors import RecordingException
import discord.gateway as discord_gateway

from .config import Settings, load_settings
from .lmstudio_client import LMStudioClient
from .processor import SessionProcessor
from .storage import GuildSettingsStore
from .whisper_client import WhisperClient


DISCORD_SAFE_LIMIT = 1900
VoiceLikeChannel = discord.VoiceChannel


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
    lmstudio = LMStudioClient(settings)
    processor = SessionProcessor(
        settings.data_dir,
        whisper,
        lmstudio,
        audio_normalize=settings.audio_normalize,
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

    def stop_background_tasks(state: GuildRecordingState) -> None:
        if state.health_task is not None:
            state.health_task.cancel()
            state.health_task = None
        if state.rotation_task is not None:
            state.rotation_task.cancel()
            state.rotation_task = None

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
        print(f"Logged in as {bot.user} (id={bot.user.id})")

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

    @bot.slash_command(name="chronicle_setup", description="Set text channel for chronicle reports")
    async def chronicle_setup(
        ctx: discord.ApplicationContext,
        channel: discord.Option(
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
        channel: discord.Option(
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
        voice_channel: discord.Option(
            input_type=discord.SlashCommandOptionType.channel,
            description="Voice channel for recording",
            channel_types=[discord.ChannelType.voice],
            required=True,
        ),
        transcript_channel: discord.Option(
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

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.followup.send("Recording already running for this guild.", ephemeral=True)
            return
        if state.processing:
            await ctx.followup.send("Previous recording is still processing.", ephemeral=True)
            return
        state.finalizing = False
        state.segment_sinks = []
        stop_background_tasks(state)

        async def on_finished(
            finished_sink: discord.sinks.Sink,
            fallback_channel: discord.abc.Messageable,
            guild_id: int,
        ) -> None:
            print(f"[on_finished] called guild={guild_id} tracks={len(finished_sink.audio_data)}")
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
                    await try_send(target_channel, "Recording segment rotated and resumed.")
                except Exception as exc:
                    await try_send(
                        target_channel,
                        f"Rotation restart failed: `{exc}`. Use `/chronicle_start` to continue.",
                    )
                return

            state.processing = True
            try:
                if not state.segment_sinks:
                    sent_no_audio = await try_send(
                        target_channel, "Recording finished, but no audio data was captured."
                    )
                    if not sent_no_audio and target_channel is not fallback_channel:
                        await try_send(
                            fallback_channel, "Recording finished, but no audio data was captured."
                        )
                    print(f"[on_finished] no audio captured guild={guild_id}")
                    return

                sent = await try_send(target_channel, "Processing recording: Whisper transcription + local LLM summary...")
                if not sent and target_channel is not fallback_channel:
                    await try_send(fallback_channel, "Processing recording: Whisper transcription + local LLM summary...")
                summary_language = store.get_summary_language(guild_id, default="ru")
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
                await try_send(target_channel, "## AI Session Summary")
                try:
                    await send_long(target_channel, artifacts.summary_markdown)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    if target_channel is not fallback_channel:
                        await send_long(fallback_channel, artifacts.summary_markdown)
            except TimeoutError:
                await try_send(
                    fallback_channel,
                    "Processing timed out. Check Whisper/LLM availability and bot logs.",
                )
            except Exception as exc:
                sent = await try_send(fallback_channel, f"Error while processing recording: `{exc}`")
                if not sent:
                    print(f"[on_finished] processing error: {exc}")
            finally:
                state.processing = False
                state.finalizing = False
                state.voice_channel_id = None
                state.segment_sinks = []
                stop_background_tasks(state)
                guild = bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    await guild.voice_client.disconnect(force=False)

        async def rotation_loop(
            guild_id: int,
            fallback_channel: discord.abc.Messageable,
        ) -> None:
            if settings.recording_rotation_seconds <= 0:
                return
            while True:
                await asyncio.sleep(settings.recording_rotation_seconds)
                state = guild_state.setdefault(guild_id, GuildRecordingState())
                if state.sink is None or state.processing or state.finalizing:
                    return
                guild = bot.get_guild(guild_id)
                if guild is None or guild.voice_client is None:
                    continue
                await try_send(
                    fallback_channel,
                    "Rotating recording segment...",
                )
                try:
                    guild.voice_client.stop_recording()
                except Exception as exc:
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
            while True:
                await asyncio.sleep(12.0)
                state = guild_state.setdefault(guild_id, GuildRecordingState())
                if state.sink is None or state.processing:
                    return

                guild = bot.get_guild(guild_id)
                if guild is None:
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

                await try_send(
                    fallback_channel,
                    "Voice connection looks unstable. Attempting automatic reconnect...",
                )
                try:
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
                except Exception as exc:
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
        except (RecordingException, RuntimeError) as exc:
            state.sink = None
            state.voice_channel_id = None
            state.finalizing = False
            stop_background_tasks(state)
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
            stop_background_tasks(state)
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
        stop_background_tasks(state)
        await ctx.respond("Disconnected from voice channel.", ephemeral=True)

    return bot


def main() -> None:
    settings = load_settings()
    bot = build_bot(settings)
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
