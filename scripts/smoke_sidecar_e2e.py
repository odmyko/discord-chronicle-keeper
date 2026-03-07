from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle_keeper.voice_sidecar_client import VoiceSidecarClient  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Manual E2E smoke for sidecar runtime: start, optional rotate, stop, "
            "then validate recorded WAV files in local data dir."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("VOICE_SIDECAR_BASE_URL", "http://127.0.0.1:8081"),
    )
    parser.add_argument("--token", default=os.getenv("SIDECAR_TOKEN", ""))
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--voice-channel-id", type=int, required=True)
    parser.add_argument("--text-channel-id", type=int, default=0)
    parser.add_argument("--requested-by", type=int, default=0)
    parser.add_argument("--session-id", default="")
    parser.add_argument(
        "--speak-seconds",
        type=float,
        default=15.0,
        help="Pause between start and stop for live speech in the channel.",
    )
    parser.add_argument(
        "--rotate-once",
        action="store_true",
        help="Send one manual rotate request before stop.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Local data dir to verify recorded files.",
    )
    return parser


async def _run() -> int:
    args = _build_parser().parse_args()
    session_id = args.session_id.strip() or "smoke_" + str(
        int(asyncio.get_event_loop().time())
    )
    client = VoiceSidecarClient(
        base_url=args.base_url,
        token=args.token,
        timeout_seconds=20.0,
    )

    print("[smoke-sidecar-e2e] health...")
    health = await client.health()
    print(health)

    print("[smoke-sidecar-e2e] start...")
    started = await client.start_session(
        {
            "guild_id": args.guild_id,
            "voice_channel_id": args.voice_channel_id,
            "text_channel_id": args.text_channel_id or None,
            "requested_by": args.requested_by or None,
            "campaign_id": "smoke",
            "campaign_name": "Smoke Campaign",
            "summary_language": "ru",
            "session_context": "smoke test",
            "name_hints": "",
            "session_id": session_id,
        }
    )
    print(started)

    print(
        f"[smoke-sidecar-e2e] waiting {args.speak_seconds:.1f}s (speak in voice channel now)..."
    )
    await asyncio.sleep(max(0.0, args.speak_seconds))

    if args.rotate_once:
        print("[smoke-sidecar-e2e] rotate...")
        rotated = await client.rotate_session(args.guild_id, reason="smoke")
        print(rotated)

    print("[smoke-sidecar-e2e] stop...")
    stopped = await client.stop_session(args.guild_id, reason="smoke")
    print(stopped)

    audio_dir = args.data_dir / "sessions" / str(args.guild_id) / session_id / "audio"
    wav_files = sorted(audio_dir.glob("*.wav"))
    print(f"[smoke-sidecar-e2e] local audio dir: {audio_dir}")
    print(f"[smoke-sidecar-e2e] wav files: {len(wav_files)}")
    for path in wav_files[:10]:
        print(f" - {path.name} ({path.stat().st_size} bytes)")

    if not wav_files:
        print(
            "[smoke-sidecar-e2e] warning: no WAV files found. Ensure someone spoke in channel during the wait window."
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
