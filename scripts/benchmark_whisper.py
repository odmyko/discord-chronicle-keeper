from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import time

import aiohttp


def find_latest_mp3(root: Path) -> Path | None:
    candidates = sorted(
        root.rglob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None
    for path in candidates:
        if path.name.lower() == "mixed_session.mp3":
            return path
    return candidates[0]


def probe_duration_seconds(path: Path) -> float | None:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nokey=1:noprint_wrappers=1",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    try:
        return float(proc.stdout.strip())
    except (TypeError, ValueError):
        return None


async def _single_run(
    endpoint: str,
    api_style: str,
    params: dict[str, str],
    model: str,
    audio_path: Path,
) -> tuple[int, str]:
    form = aiohttp.FormData()
    with audio_path.open("rb") as fh:
        content_type = (
            "audio/mpeg" if audio_path.suffix.lower() == ".mp3" else "audio/wav"
        )
        if api_style == "openai":
            form.add_field(
                "file", fh, filename=audio_path.name, content_type=content_type
            )
            form.add_field("model", model)
            form.add_field("response_format", "verbose_json")
            form.add_field("timestamp_granularities[]", "segment")
            if params.get("language"):
                form.add_field("language", params["language"])
        else:
            form.add_field(
                "audio_file", fh, filename=audio_path.name, content_type=content_type
            )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                endpoint, params=params, data=form, timeout=600
            ) as response:
                return response.status, await response.text()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Whisper endpoint on a real recorded mp3."
    )
    parser.add_argument(
        "--whisper-url", default="http://127.0.0.1:9000", help="Whisper base URL"
    )
    parser.add_argument(
        "--api-style",
        choices=["asr", "openai"],
        default="asr",
        help="ASR endpoint style",
    )
    parser.add_argument("--asr-path", default="/asr", help="ASR path")
    parser.add_argument(
        "--model",
        default="openai/whisper-large-v3-turbo",
        help="Model name for openai API style",
    )
    parser.add_argument(
        "--audio", type=Path, default=None, help="Input audio file (.mp3/.wav)"
    )
    parser.add_argument(
        "--search-root",
        type=Path,
        default=Path("data/sessions"),
        help="Where to find latest mp3",
    )
    parser.add_argument(
        "--language", default="ru", help="language query param for Whisper"
    )
    parser.add_argument("--task", default="transcribe", help="Whisper task")
    parser.add_argument(
        "--encode",
        default="true",
        choices=["true", "false"],
        help="Whisper encode query param",
    )
    parser.add_argument(
        "--runs", type=int, default=1, help="How many benchmark runs to perform"
    )
    args = parser.parse_args()

    audio_path = args.audio
    if audio_path is None:
        audio_path = find_latest_mp3(args.search_root)
    if audio_path is None or not audio_path.exists():
        raise SystemExit(
            "No audio file found. Provide --audio or place mp3 files under data/sessions."
        )

    endpoint = f"{args.whisper_url.rstrip('/')}{args.asr_path}"
    if args.api_style == "openai":
        params = {}
        if args.language:
            params["language"] = args.language
    else:
        params = {
            "task": args.task,
            "encode": args.encode,
            "output": "json",
        }
        if args.language:
            params["language"] = args.language

    duration = probe_duration_seconds(audio_path)
    print(f"Audio: {audio_path}")
    if duration is not None:
        print(f"Audio duration: {duration:.2f}s")

    timings: list[float] = []
    for idx in range(1, max(1, args.runs) + 1):
        started = time.perf_counter()
        status, body = asyncio.run(
            _single_run(endpoint, args.api_style, params, args.model, audio_path)
        )
        elapsed = time.perf_counter() - started
        timings.append(elapsed)

        if status >= 400:
            print(f"Run {idx}: ERROR {status}: {body[:500]}")
            continue

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"text": body}
        text = str(payload.get("text", "")).strip()
        segs = payload.get("segments")
        seg_count = len(segs) if isinstance(segs, list) else 0
        rtf = (elapsed / duration) if duration and duration > 0 else None
        rtf_str = f"{rtf:.3f}" if rtf is not None else "n/a"
        print(
            f"Run {idx}: {elapsed:.2f}s, rtf={rtf_str}, chars={len(text)}, segments={seg_count}"
        )

    if timings:
        avg = sum(timings) / len(timings)
        print(f"Average: {avg:.2f}s across {len(timings)} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
