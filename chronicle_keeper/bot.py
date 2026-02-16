from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

import discord
from discord.ext import commands

from .config import Settings, load_settings
from .lmstudio_client import LMStudioClient
from .processor import SessionProcessor
from .storage import GuildSettingsStore
from .whisper_client import WhisperClient


DISCORD_SAFE_LIMIT = 1900


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


def build_bot(settings: Settings) -> commands.Bot:
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    intents = discord.Intents.default()
    intents.voice_states = True
    intents.guilds = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    store = GuildSettingsStore(settings.data_dir / "guild_settings.json")
    whisper = WhisperClient(settings)
    lmstudio = LMStudioClient(settings)
    processor = SessionProcessor(settings.data_dir, whisper, lmstudio)
    guild_state: dict[int, GuildRecordingState] = {}

    async def send_long(channel: discord.abc.Messageable, text: str) -> None:
        for chunk in chunk_text(text):
            await channel.send(chunk)

    @bot.event
    async def on_ready() -> None:
        print(f"Logged in as {bot.user} (id={bot.user.id})")

    @bot.slash_command(name="chronicle_setup", description="Set text channel for chronicle reports")
    async def chronicle_setup(ctx: discord.ApplicationContext, channel: discord.TextChannel) -> None:
        if ctx.guild is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        store.set_chronicle_channel(ctx.guild.id, channel.id)
        await ctx.respond(f"Chronicle channel set to {channel.mention}.", ephemeral=True)

    @bot.slash_command(name="chronicle_start", description="Join your voice channel and start recording")
    async def chronicle_start(ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.user is None:
            await ctx.respond("This command can be used only in a server.", ephemeral=True)
            return

        if not isinstance(ctx.user, discord.Member) or not ctx.user.voice or not ctx.user.voice.channel:
            await ctx.respond("Join a voice channel first.", ephemeral=True)
            return

        state = guild_state.setdefault(ctx.guild.id, GuildRecordingState())
        if state.sink is not None:
            await ctx.respond("Recording already running for this guild.", ephemeral=True)
            return
        if state.processing:
            await ctx.respond("Previous recording is still processing.", ephemeral=True)
            return

        voice_channel = ctx.user.voice.channel
        voice_client = ctx.guild.voice_client
        if voice_client is None:
            voice_client = await voice_channel.connect()
        elif voice_client.channel.id != voice_channel.id:
            await voice_client.move_to(voice_channel)

        sink = discord.sinks.WaveSink()
        state.sink = sink

        async def on_finished(finished_sink: discord.sinks.Sink, _text_channel: discord.TextChannel, guild_id: int) -> None:
            state = guild_state.setdefault(guild_id, GuildRecordingState())
            state.sink = None
            state.processing = True
            try:
                guild = bot.get_guild(guild_id)
                if guild is None:
                    return

                chronicle_channel_id = store.get_chronicle_channel(guild_id)
                if chronicle_channel_id is None:
                    return
                target_channel = guild.get_channel(chronicle_channel_id)
                if not isinstance(target_channel, discord.TextChannel):
                    return

                if not finished_sink.audio_data:
                    await target_channel.send("Recording finished, but no audio data was captured.")
                    return

                await target_channel.send("Processing recording: Whisper transcription + LM Studio summary...")
                artifacts = await processor.process_sink(guild, finished_sink)

                await target_channel.send(f"Session saved: `{artifacts.session_dir}`")
                await target_channel.send("## Full Transcript")
                await send_long(target_channel, artifacts.full_transcript)
                await target_channel.send("## AI Session Summary")
                await send_long(target_channel, artifacts.summary_markdown)
            except Exception as exc:
                guild = bot.get_guild(guild_id)
                channel_id = store.get_chronicle_channel(guild_id)
                if guild and channel_id:
                    channel = guild.get_channel(channel_id)
                    if isinstance(channel, discord.TextChannel):
                        await channel.send(f"Error while processing recording: `{exc}`")
            finally:
                state.processing = False
                guild = bot.get_guild(guild_id)
                if guild and guild.voice_client:
                    await guild.voice_client.disconnect(force=False)

        voice_client.start_recording(sink, on_finished, ctx.channel, ctx.guild.id)
        await ctx.respond(f"Recording started in {voice_channel.mention}.", ephemeral=True)

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
        await ctx.respond("Disconnected from voice channel.", ephemeral=True)

    return bot


def main() -> None:
    settings = load_settings()
    bot = build_bot(settings)
    bot.run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
