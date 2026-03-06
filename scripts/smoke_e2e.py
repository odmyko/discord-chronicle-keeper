from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chronicle_keeper.config import load_settings  # noqa: E402
from chronicle_keeper.asr import create_asr_client  # noqa: E402
from chronicle_keeper.llm_client import LLMClient  # noqa: E402


def _find_latest_audio(search_root: Path) -> Path | None:
    if not search_root.exists():
        return None
    candidates = sorted(
        search_root.rglob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None
    for candidate in candidates:
        if candidate.name.lower() == "mixed_session.mp3":
            return candidate
    return candidates[0]


async def _run(audio_path: Path) -> int:
    settings = load_settings()
    asr = create_asr_client(settings)
    llm = LLMClient(settings)

    started = time.perf_counter()
    transcript = await asr.transcribe_file_detailed(audio_path)
    asr_s = time.perf_counter() - started

    summary_input = transcript.text.strip() or "No speech detected in smoke test audio."
    started = time.perf_counter()
    summary = await llm.generate_summary(summary_input, language="ru")
    llm_s = time.perf_counter() - started

    print("SMOKE PASS")
    print(f"audio={audio_path}")
    print(
        f"asr_chars={len(transcript.text.strip())} asr_segments={len(transcript.segments)} asr_time_s={asr_s:.2f}"
    )
    print(f"summary_chars={len(summary)} llm_time_s={llm_s:.2f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Smoke-test end-to-end ASR + LLM flow."
    )
    parser.add_argument(
        "--audio", type=Path, default=None, help="Audio file path (.mp3/.wav)."
    )
    parser.add_argument(
        "--search-root",
        type=Path,
        default=Path("data/sessions"),
        help="Search root for latest session audio.",
    )
    args = parser.parse_args()

    audio = args.audio or _find_latest_audio(args.search_root)
    if audio is None or not audio.exists():
        print(
            "SMOKE FAIL: no audio file found. Pass --audio or place recorded audio under data/sessions."
        )
        return 2

    try:
        return asyncio.run(_run(audio))
    except Exception as exc:
        print(f"SMOKE FAIL: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
