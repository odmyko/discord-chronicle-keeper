from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .config import load_settings
from .asr import create_asr_client
from .llm_client import LLMClient
from .processor import SessionProcessor


logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reprocess a saved session from existing audio artifacts.",
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="Path to session directory, e.g. data/sessions/<guild_id>/<session_id>",
    )
    parser.add_argument(
        "--guild-id",
        type=int,
        help="Guild ID (used with --session-id).",
    )
    parser.add_argument(
        "--session-id",
        type=str,
        help="Session folder id, e.g. 20260219_201349 (used with --guild-id).",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="ru",
        choices=["en", "uk", "ru"],
        help="Summary language (default: ru).",
    )
    parser.add_argument(
        "--audio-subdir",
        type=str,
        default="",
        help=(
            "Session audio subdirectory to process (e.g. 'audio' or 'audio_vad'). "
            "If omitted, reprocess auto-selects based on settings."
        ),
    )
    return parser


def _resolve_session_dir(args: argparse.Namespace, data_dir: Path) -> Path:
    if args.session_dir:
        return args.session_dir
    if args.guild_id is not None and args.session_id:
        return data_dir / "sessions" / str(args.guild_id) / args.session_id
    raise RuntimeError("Provide either --session-dir OR --guild-id with --session-id.")


async def _run() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    settings = load_settings()
    session_dir = _resolve_session_dir(args, settings.data_dir)
    if not session_dir.exists() or not session_dir.is_dir():
        raise RuntimeError(f"Session directory not found: {session_dir}")

    asr_client = create_asr_client(settings)
    llm = LLMClient(settings)
    processor = SessionProcessor(
        settings.data_dir,
        asr_client,
        llm,
        audio_dual_pipeline_enabled=settings.audio_dual_pipeline_enabled,
        audio_normalize=settings.audio_normalize,
        audio_vad_enabled=settings.audio_vad_enabled,
        audio_target_sample_rate=settings.audio_target_sample_rate,
        audio_target_channels=settings.audio_target_channels,
        audio_mp3_vbr_quality=settings.audio_mp3_vbr_quality,
        summary_context_relevance_gate=settings.summary_context_relevance_gate,
        summary_context_min_relevance=settings.summary_context_min_relevance,
    )

    logger.info(
        "[reprocess-cli] start session_dir=%s language=%s audio_subdir=%s",
        session_dir,
        args.language,
        (args.audio_subdir or "<auto>"),
    )
    artifacts = await processor.reprocess_saved_session(
        session_dir=session_dir,
        summary_language=args.language,
        audio_subdir=(args.audio_subdir or None),
    )
    logger.info(
        "[reprocess-cli] done session_dir=%s transcript=%s summary=%s",
        artifacts.session_dir,
        artifacts.full_transcript_txt_path.name,
        artifacts.summary_path.name,
    )
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
