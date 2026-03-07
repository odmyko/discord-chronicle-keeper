from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import shutil
import subprocess
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle_keeper.asr import create_asr_client  # noqa: E402
from chronicle_keeper.config import load_settings  # noqa: E402
from chronicle_keeper.llm_client import LLMClient  # noqa: E402
from chronicle_keeper.processor import SessionProcessor  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Local smoke test for ASR+summary pipeline without Discord runtime. "
            "Can optionally split source audio into chunk-like segments."
        )
    )
    parser.add_argument(
        "--audio",
        type=Path,
        required=True,
        help="Source audio file (mp3/wav/m4a/etc).",
    )
    parser.add_argument(
        "--language",
        type=str,
        default="ru",
        choices=["en", "uk", "ru"],
        help="Summary language.",
    )
    parser.add_argument(
        "--segment-seconds",
        type=int,
        default=0,
        help=(
            "If >0, split source audio with ffmpeg into N-second segments "
            "(emulates recording rotation)."
        ),
    )
    parser.add_argument(
        "--mode",
        type=str,
        default="full",
        choices=["full", "split"],
        help=(
            "`full`: one-pass reprocess (ASR+summary). "
            "`split`: transcribe-only first, then summary-only."
        ),
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        default=None,
        help=(
            "Optional target session dir. If omitted, creates "
            "data/sessions/0/<utc_session_id>."
        ),
    )
    parser.add_argument(
        "--speaker-name",
        type=str,
        default="smoketest",
        help="Speaker label used in generated chunk filenames.",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=111111111111111111,
        help="Fake user id used in generated chunk filenames.",
    )
    parser.add_argument(
        "--campaign-id",
        type=str,
        default="",
        help="Optional campaign id for summary context.",
    )
    parser.add_argument(
        "--campaign-name",
        type=str,
        default="",
        help="Optional campaign name for summary context.",
    )
    parser.add_argument(
        "--session-context",
        type=str,
        default="",
        help="Optional world/session context passed into summarizer.",
    )
    parser.add_argument(
        "--name-hints",
        type=str,
        default="",
        help="Optional name hints passed into summarizer.",
    )
    return parser


def _utc_session_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _copy_as_single_chunk(
    source_audio: Path,
    audio_dir: Path,
    speaker_name: str,
    user_id: int,
) -> list[Path]:
    suffix = source_audio.suffix or ".mp3"
    out = audio_dir / f"{speaker_name}_{user_id}_seg001{suffix}"
    shutil.copy2(source_audio, out)
    return [out]


def _split_into_chunks(
    source_audio: Path,
    audio_dir: Path,
    segment_seconds: int,
    speaker_name: str,
    user_id: int,
) -> list[Path]:
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError(
            "ffmpeg is required for --segment-seconds mode, but was not found in PATH."
        )

    tmp_pattern = audio_dir / "tmp_seg_%03d.mp3"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(source_audio),
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "4",
        str(tmp_pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg segmentation failed (code {result.returncode}):\n{result.stderr}"
        )

    tmp_files = sorted(audio_dir.glob("tmp_seg_*.mp3"))
    if not tmp_files:
        raise RuntimeError("ffmpeg produced no segments.")

    out_files: list[Path] = []
    for idx, src in enumerate(tmp_files, start=1):
        dst = audio_dir / f"{speaker_name}_{user_id}_seg{idx:03d}.mp3"
        src.replace(dst)
        out_files.append(dst)
    return out_files


async def _run() -> int:
    args = _build_parser().parse_args()
    if not args.audio.exists() or not args.audio.is_file():
        raise RuntimeError(f"Audio file not found: {args.audio}")

    settings = load_settings()
    session_dir = (
        args.session_dir
        if args.session_dir is not None
        else settings.data_dir / "sessions" / "0" / _utc_session_id()
    )
    audio_dir = session_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    if args.segment_seconds > 0:
        chunk_files = _split_into_chunks(
            args.audio,
            audio_dir,
            segment_seconds=args.segment_seconds,
            speaker_name=args.speaker_name,
            user_id=args.user_id,
        )
    else:
        chunk_files = _copy_as_single_chunk(
            args.audio,
            audio_dir,
            speaker_name=args.speaker_name,
            user_id=args.user_id,
        )

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

    print(f"[smoke-local] session_dir={session_dir}")
    print(
        f"[smoke-local] chunks_prepared={len(chunk_files)} "
        f"(segment_seconds={args.segment_seconds})"
    )

    if args.mode == "split":
        processed, total = await processor.transcribe_saved_session_incremental(
            session_dir=session_dir,
            audio_subdir="audio",
            force=False,
        )
        print(f"[smoke-local] transcribe-only processed={processed} total={total}")
        artifacts = await processor.resummarize_saved_session(
            session_dir=session_dir,
            summary_language=args.language,
            session_context=args.session_context,
            name_hints=args.name_hints,
        )
    else:
        artifacts = await processor.reprocess_saved_session(
            session_dir=session_dir,
            summary_language=args.language,
            session_context=args.session_context,
            name_hints=args.name_hints,
            campaign_id=args.campaign_id,
            campaign_name=args.campaign_name,
            audio_subdir="audio",
        )

    print(
        "[smoke-local] done "
        f"transcript={artifacts.full_transcript_txt_path} summary={artifacts.summary_path}"
    )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
