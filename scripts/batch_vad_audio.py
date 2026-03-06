from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch VAD-like silence trimming using ffmpeg silenceremove."
    )
    parser.add_argument("--input-dir", type=Path, required=True, help="Input folder")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output folder")
    parser.add_argument(
        "--glob", default="*.mp3", help="Input glob pattern (default: *.mp3)"
    )
    parser.add_argument(
        "--sample-rate", type=int, default=16000, help="Output sample rate"
    )
    parser.add_argument("--channels", type=int, default=1, help="Output channels")
    parser.add_argument(
        "--bitrate", default="96k", help="Audio bitrate for compressed outputs"
    )
    parser.add_argument(
        "--start-duration",
        type=float,
        default=0.25,
        help="Start silence duration threshold (seconds)",
    )
    parser.add_argument(
        "--stop-duration",
        type=float,
        default=0.35,
        help="Stop silence duration threshold (seconds)",
    )
    parser.add_argument(
        "--threshold-db",
        type=float,
        default=-45.0,
        help="Silence threshold in dB (default: -45)",
    )
    args = parser.parse_args()

    if not args.input_dir.exists():
        raise SystemExit(f"Input directory not found: {args.input_dir}")

    files = sorted(args.input_dir.glob(args.glob))
    if not files:
        raise SystemExit(f"No files matched '{args.glob}' in {args.input_dir}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    th = f"{args.threshold_db}dB"
    af = (
        "silenceremove="
        f"start_periods=1:start_duration={args.start_duration}:start_threshold={th}:"
        f"stop_periods=-1:stop_duration={args.stop_duration}:stop_threshold={th},"
        "asetpts=N/SR/TB"
    )

    print(f"input_count={len(files)}")
    print(f"output_dir={args.output_dir}")
    ok = 0
    for src in files:
        dst = args.output_dir / src.name
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-af",
            af,
            "-ar",
            str(args.sample_rate),
            "-ac",
            str(args.channels),
            "-b:a",
            args.bitrate,
            str(dst),
        ]
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
        )
        if proc.returncode == 0:
            ok += 1
            print(f"ok {src.name}")
        else:
            print(f"fail {src.name} code={proc.returncode}")

    print(f"done ok={ok} fail={len(files) - ok}")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
