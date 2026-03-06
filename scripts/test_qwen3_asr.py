from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any


def _format_srt_time(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _default_dtype(torch_module: Any) -> Any:
    if torch_module.cuda.is_available():
        return torch_module.bfloat16
    return torch_module.float32


def _dtype_from_name(torch_module: Any, name: str) -> Any:
    value = name.lower()
    if value == "auto":
        return _default_dtype(torch_module)
    if value == "bfloat16":
        return torch_module.bfloat16
    if value == "float16":
        return torch_module.float16
    if value == "float32":
        return torch_module.float32
    raise ValueError(f"Unsupported dtype: {name}")


def _torch_info(torch_module: Any) -> dict[str, Any]:
    cuda_available = torch_module.cuda.is_available()
    info: dict[str, Any] = {
        "torch_version": getattr(torch_module, "__version__", "unknown"),
        "cuda_available": cuda_available,
        "cuda_version": getattr(torch_module.version, "cuda", None),
        "device_count": torch_module.cuda.device_count() if cuda_available else 0,
    }
    if cuda_available:
        info["device_name"] = torch_module.cuda.get_device_name(0)
    return info


def _serialize_time_stamps(time_stamps: Any) -> list[dict[str, Any]] | None:
    if time_stamps is None:
        return None
    items = getattr(time_stamps, "items", None)
    if items is None:
        return None
    rows: list[dict[str, Any]] = []
    for item in items:
        rows.append(
            {
                "text": getattr(item, "text", None),
                "start_time": float(getattr(item, "start_time", 0.0)),
                "end_time": float(getattr(item, "end_time", 0.0)),
            }
        )
    return rows


def _result_to_dict(item: Any) -> dict[str, Any]:
    payload = {
        "language": getattr(item, "language", None),
        "text": getattr(item, "text", None),
    }
    time_stamps = _serialize_time_stamps(getattr(item, "time_stamps", None))
    if time_stamps is not None:
        payload["time_stamps"] = time_stamps
    return payload


def _write_tsv(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["start_time	end_time	text"]
    for row in rows:
        lines.append(
            f"{row['start_time']:.3f}	{row['end_time']:.3f}	{row['text']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_srt(
    path: Path, rows: list[dict[str, Any]], max_words: int = 10, max_gap: float = 1.2
) -> None:
    blocks: list[tuple[float, float, str]] = []
    current: list[dict[str, Any]] = []
    for row in rows:
        if not current:
            current.append(row)
            continue
        gap = row["start_time"] - current[-1]["end_time"]
        if gap > max_gap or len(current) >= max_words:
            text = " ".join(str(item["text"]) for item in current if item.get("text"))
            blocks.append((current[0]["start_time"], current[-1]["end_time"], text))
            current = [row]
        else:
            current.append(row)
    if current:
        text = " ".join(str(item["text"]) for item in current if item.get("text"))
        blocks.append((current[0]["start_time"], current[-1]["end_time"], text))

    parts: list[str] = []
    for idx, (start, end, content) in enumerate(blocks, start=1):
        parts.append(str(idx))
        parts.append(f"{_format_srt_time(start)} --> {_format_srt_time(end)}")
        parts.append(content)
        parts.append("")
    path.write_text("\n".join(parts), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a local audio file through Qwen3-ASR and print the transcript."
    )
    parser.add_argument("--audio", type=Path, help="Input audio file")
    parser.add_argument(
        "--audio-dir", type=Path, help="Input directory with audio files"
    )
    parser.add_argument(
        "--glob",
        default="*.mp3",
        help="Glob pattern for --audio-dir (default: *.mp3)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Optional output directory for per-file transcripts (*.txt)",
    )
    parser.add_argument(
        "--model", default="Qwen/Qwen3-ASR-1.7B", help="Hugging Face model id"
    )
    parser.add_argument(
        "--backend",
        choices=["transformers", "vllm"],
        default="transformers",
        help="Inference backend exposed by qwen-asr",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional spoken language hint, e.g. Russian or English",
    )
    parser.add_argument(
        "--dtype",
        choices=["auto", "bfloat16", "float16", "float32"],
        default="auto",
        help="Model dtype",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=4096,
        help="Maximum decoder output tokens",
    )
    parser.add_argument(
        "--max-inference-batch-size",
        type=int,
        default=32,
        help="qwen-asr internal inference batch size",
    )
    parser.add_argument(
        "--gpu-memory-utilization",
        type=float,
        default=0.8,
        help="vLLM only: target GPU memory utilization",
    )
    parser.add_argument(
        "--forced-aligner",
        default=None,
        help="Optional forced aligner model id for timestamps",
    )
    parser.add_argument(
        "--return-time-stamps",
        action="store_true",
        help="Return timestamps when forced aligner is configured",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full result payload as JSON instead of plain text",
    )
    parser.add_argument(
        "--tsv-out",
        type=Path,
        default=None,
        help="Optional output path for word-level timestamps as TSV",
    )
    parser.add_argument(
        "--srt-out",
        type=Path,
        default=None,
        help="Optional output path for grouped subtitles as SRT",
    )
    args = parser.parse_args()

    if args.audio is None and args.audio_dir is None:
        raise SystemExit("Provide either --audio or --audio-dir.")
    if args.audio is not None and args.audio_dir is not None:
        raise SystemExit("Use only one of --audio or --audio-dir.")
    if args.audio is not None and not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")
    if args.audio_dir is not None and not args.audio_dir.exists():
        raise SystemExit(f"Audio directory not found: {args.audio_dir}")

    _load_env_file(Path(".env"))
    os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")
    os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

    import torch
    from qwen_asr import Qwen3ASRModel

    dtype = _dtype_from_name(torch, args.dtype)
    device_map = "cuda:0" if torch.cuda.is_available() else "cpu"
    load_kwargs: dict[str, Any] = {
        "max_inference_batch_size": args.max_inference_batch_size,
        "max_new_tokens": args.max_new_tokens,
    }
    if args.backend == "transformers":
        load_kwargs["dtype"] = dtype
        load_kwargs["device_map"] = device_map
        if args.forced_aligner:
            load_kwargs["forced_aligner"] = args.forced_aligner
            load_kwargs["forced_aligner_kwargs"] = {
                "dtype": dtype,
                "device_map": device_map,
            }
        model = Qwen3ASRModel.from_pretrained(args.model, **load_kwargs)
    else:
        if args.forced_aligner:
            load_kwargs["forced_aligner"] = args.forced_aligner
            load_kwargs["forced_aligner_kwargs"] = {
                "dtype": dtype,
                "device_map": device_map,
            }
        model = Qwen3ASRModel.LLM(
            model=args.model,
            gpu_memory_utilization=args.gpu_memory_utilization,
            **load_kwargs,
        )

    print(f"model={args.model}")
    print(f"backend={args.backend}")
    print(f"torch={json.dumps(_torch_info(torch), ensure_ascii=False)}")
    print(f"dtype={str(dtype).replace('torch.', '')}")
    print(f"max_new_tokens={args.max_new_tokens}")
    if args.language:
        print(f"language_hint={args.language}")

    def transcribe_one(audio_path: Path) -> None:
        started = time.perf_counter()
        result = model.transcribe(
            audio=str(audio_path),
            language=args.language,
            return_time_stamps=args.return_time_stamps,
        )
        elapsed = time.perf_counter() - started
        first = result[0]
        rows = _serialize_time_stamps(getattr(first, "time_stamps", None))
        text = str(getattr(first, "text", "") or "")

        if args.out_dir is not None:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            (args.out_dir / f"{audio_path.stem}.txt").write_text(text, encoding="utf-8")

        print(f"audio={audio_path}")
        print(f"elapsed_s={elapsed:.2f}")
        if args.json:
            print("result_json:")
            print(json.dumps(_result_to_dict(first), ensure_ascii=False, indent=2))
        else:
            print("language:")
            print(getattr(first, "language", ""))
            print("text:")
            print(text)
            if args.return_time_stamps and rows is not None:
                print("time_stamps:")
                for row in rows[:80]:
                    print(
                        f"[{row['start_time']:.3f} -> {row['end_time']:.3f}] {row['text']}"
                    )
                if len(rows) > 80:
                    print(f"... ({len(rows) - 80} more words omitted)")
        if args.tsv_out and rows is not None:
            _write_tsv(args.tsv_out, rows)
            print(f"tsv_out={args.tsv_out}")
        if args.srt_out and rows is not None:
            _write_srt(args.srt_out, rows)
            print(f"srt_out={args.srt_out}")
        print("-" * 60)

    if args.audio is not None:
        transcribe_one(args.audio)
    else:
        audio_files = sorted(args.audio_dir.glob(args.glob))
        if not audio_files:
            raise SystemExit(
                f"No files matched in {args.audio_dir} with glob '{args.glob}'"
            )
        print(f"batch_count={len(audio_files)}")
        for audio_path in audio_files:
            transcribe_one(audio_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
