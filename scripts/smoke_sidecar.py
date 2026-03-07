from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle_keeper.voice_sidecar_client import VoiceSidecarClient  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke-test Node voice sidecar control API contract.",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("VOICE_SIDECAR_BASE_URL", "http://127.0.0.1:8081"),
        help="Sidecar base URL.",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("SIDECAR_TOKEN", ""),
        help="Optional sidecar auth token.",
    )
    parser.add_argument("--guild-id", type=int, default=900000000000000001)
    parser.add_argument("--voice-channel-id", type=int, default=900000000000000002)
    parser.add_argument("--text-channel-id", type=int, default=900000000000000003)
    parser.add_argument("--requested-by", type=int, default=900000000000000004)
    parser.add_argument("--summary-language", default="ru")
    return parser


async def _run() -> int:
    args = _build_parser().parse_args()
    client = VoiceSidecarClient(
        base_url=args.base_url,
        token=args.token,
        timeout_seconds=15.0,
    )

    print("== health ==")
    print(json.dumps(await client.health(), indent=2, ensure_ascii=False))

    print("== start ==")
    started = await client.start_session(
        {
            "guild_id": args.guild_id,
            "voice_channel_id": args.voice_channel_id,
            "text_channel_id": args.text_channel_id,
            "requested_by": args.requested_by,
            "campaign_id": "smoke",
            "campaign_name": "Smoke Campaign",
            "summary_language": args.summary_language,
            "session_context": "smoke test",
            "name_hints": "n/a",
        }
    )
    print(json.dumps(started, indent=2, ensure_ascii=False))

    print("== status ==")
    print(
        json.dumps(
            await client.session_status(args.guild_id), indent=2, ensure_ascii=False
        )
    )

    print("== rotate ==")
    print(
        json.dumps(
            await client.rotate_session(args.guild_id, reason="smoke"),
            indent=2,
            ensure_ascii=False,
        )
    )

    print("== stop ==")
    print(
        json.dumps(
            await client.stop_session(args.guild_id, reason="smoke"),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
