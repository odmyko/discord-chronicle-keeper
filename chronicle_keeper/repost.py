from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

import discord

from .config import load_settings
from .storage import GuildSettingsStore


logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repost already generated session artifacts to Discord channel.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Path like data/sessions/<guild_id>/<session_id>.",
    )
    parser.add_argument("--guild-id", type=int, help="Guild ID (with --session-id).")
    parser.add_argument(
        "--session-id",
        type=str,
        help="Session folder id, e.g. 20260306_164030 (with --guild-id).",
    )
    parser.add_argument(
        "--channel-id",
        type=int,
        default=0,
        help="Target text channel id. If omitted, uses guild chronicle channel.",
    )
    parser.add_argument(
        "--mention-user-id",
        type=int,
        default=0,
        help="Optional user id to mention in repost header.",
    )
    parser.add_argument(
        "--no-mixed-audio",
        action="store_true",
        help="Do not attach mixed_session.mp3 even if it exists.",
    )
    return parser


def _resolve_session_dir(args: argparse.Namespace, data_dir: Path) -> Path:
    if args.session_dir:
        return args.session_dir
    if args.guild_id is not None and args.session_id:
        return data_dir / "sessions" / str(args.guild_id) / args.session_id
    raise RuntimeError("Provide either --session-dir OR --guild-id with --session-id.")


def _resolve_guild_id(session_dir: Path) -> int | None:
    state_path = session_dir / "processing_state.json"
    if state_path.exists():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            value = payload.get("guild_id")
            if value is not None:
                return int(value)
        except Exception:
            pass
    try:
        return int(session_dir.parent.name)
    except Exception:
        return None


def _split_message(text: str, limit: int = 1900) -> list[str]:
    data = (text or "").strip()
    if not data:
        return []
    chunks: list[str] = []
    while len(data) > limit:
        idx = data.rfind("\n", 0, limit)
        if idx <= 0:
            idx = limit
        chunks.append(data[:idx])
        data = data[idx:].lstrip("\n")
    if data:
        chunks.append(data)
    return chunks


async def _send_if_exists(channel: discord.abc.Messageable, path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        await channel.send(file=discord.File(str(path), filename=path.name))
        return True
    except Exception as exc:
        logger.warning("[repost] failed to upload %s: %s", path, exc)
        return False


async def _run() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    settings = load_settings()

    session_dir = _resolve_session_dir(args, settings.data_dir)
    if not session_dir.exists() or not session_dir.is_dir():
        raise RuntimeError(f"Session directory not found: {session_dir}")

    guild_id = _resolve_guild_id(session_dir)
    if guild_id is None:
        raise RuntimeError(
            "Cannot resolve guild id from session dir. Pass --guild-id explicitly."
        )

    channel_id = int(args.channel_id or 0)
    if channel_id <= 0:
        store = GuildSettingsStore(settings.data_dir / "guild_settings.json")
        configured = store.get_chronicle_channel(guild_id)
        if configured is None:
            raise RuntimeError(
                f"No channel configured for guild {guild_id}. Pass --channel-id."
            )
        channel_id = configured

    header_prefix = (
        f"<@{int(args.mention_user_id)}> " if int(args.mention_user_id or 0) > 0 else ""
    )
    summary_path = session_dir / "summary.md"
    transcript_path = session_dir / "full_transcript.txt"
    chunk_summaries_path = session_dir / "chunk_summaries.md"
    mixed_audio_path = session_dir / "audio" / "mixed_session.mp3"
    if not mixed_audio_path.exists():
        mixed_audio_path = session_dir / "audio_vad" / "mixed_session.mp3"

    summary_body = ""
    if summary_path.exists():
        summary_body = summary_path.read_text(encoding="utf-8", errors="ignore").strip()

    client = discord.Client(intents=discord.Intents.none())
    done = asyncio.Event()
    result: dict[str, int] = {"code": 0}

    @client.event
    async def on_ready() -> None:
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                raise RuntimeError(f"Channel is not messageable: {channel_id}")

            await channel.send(
                f"{header_prefix}Reposting session `{session_dir.name}` artifacts."
            )
            sent = await _send_if_exists(channel, transcript_path)
            sent = (await _send_if_exists(channel, chunk_summaries_path)) or sent
            if not args.no_mixed_audio:
                sent = (await _send_if_exists(channel, mixed_audio_path)) or sent
            if summary_body:
                await channel.send("## AI Session Summary")
                for chunk in _split_message(summary_body):
                    await channel.send(chunk)
                sent = True
            elif summary_path.exists():
                sent = (await _send_if_exists(channel, summary_path)) or sent
            if not sent:
                await channel.send(
                    "No summary/transcript artifacts found for this session."
                )
        except Exception:
            logger.exception("[repost] failed")
            result["code"] = 1
        finally:
            done.set()
            await client.close()

    await client.login(settings.discord_bot_token)
    ws_task = asyncio.create_task(client.connect(reconnect=False))
    await done.wait()
    try:
        await ws_task
    except Exception as exc:
        # Expected on forced close during graceful shutdown in CLI mode.
        logger.debug("[repost] websocket task closed with error: %s", exc)
    return result["code"]


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
