from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _build_probe_code(
    audio: str,
    attn: str,
    max_new_tokens: int,
    max_inference_batch_size: int,
    language: str,
) -> str:
    payload = {
        "audio": audio,
        "attn": attn,
        "max_new_tokens": max_new_tokens,
        "max_inference_batch_size": max_inference_batch_size,
        "language": language,
    }
    return (
        "import json,time,torch\n"
        "from qwen_asr import Qwen3ASRModel\n"
        f"cfg=json.loads({json.dumps(json.dumps(payload))})\n"
        "t0=time.perf_counter()\n"
        "model=Qwen3ASRModel.from_pretrained(\n"
        "  'Qwen/Qwen3-ASR-1.7B',\n"
        "  dtype='float16',\n"
        "  device_map='cuda:0',\n"
        "  max_new_tokens=cfg['max_new_tokens'],\n"
        "  max_inference_batch_size=cfg['max_inference_batch_size'],\n"
        "  attn_implementation=cfg['attn'],\n"
        ")\n"
        "t1=time.perf_counter()\n"
        "out=model.transcribe(audio=cfg['audio'], language=cfg['language'], return_time_stamps=False)\n"
        "t2=time.perf_counter()\n"
        "text=out[0].text if out else ''\n"
        "print(json.dumps({\n"
        "  'torch': torch.__version__,\n"
        "  'cuda': bool(torch.cuda.is_available()),\n"
        "  'attn': cfg['attn'],\n"
        "  'load_s': round(t1-t0, 3),\n"
        "  'infer_s': round(t2-t1, 3),\n"
        "  'total_s': round(t2-t0, 3),\n"
        "  'chars': len((text or '').strip()),\n"
        "  'preview': (text or '')[:220],\n"
        "}, ensure_ascii=False))\n"
    )


def _extract_last_json(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            return json.loads(line)
    raise RuntimeError(f"Unable to find JSON payload in output:\n{stdout}")


def _run_probe(
    python_exe: Path,
    audio: Path,
    attn: str,
    max_new_tokens: int,
    max_inference_batch_size: int,
    language: str,
) -> dict:
    code = _build_probe_code(
        audio=str(audio),
        attn=attn,
        max_new_tokens=max_new_tokens,
        max_inference_batch_size=max_inference_batch_size,
        language=language,
    )
    proc = subprocess.run(
        [str(python_exe), "-u", "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "Probe failed.\n"
            f"python={python_exe}\n"
            f"attn={attn}\n"
            f"stderr:\n{proc.stderr}\n"
            f"stdout:\n{proc.stdout}"
        )
    result = _extract_last_json(proc.stdout)
    result["raw_stdout"] = proc.stdout
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark Qwen3-ASR one-shot inference between two Python envs."
    )
    parser.add_argument(
        "--audio",
        type=Path,
        required=True,
        help="Path to input audio file.",
    )
    parser.add_argument(
        "--sdpa-python",
        type=Path,
        default=Path(".venv/Scripts/python.exe"),
        help="Python executable for baseline env (sdpa).",
    )
    parser.add_argument(
        "--fa2-python",
        type=Path,
        default=Path(".venv-fa2-win283/Scripts/python.exe"),
        help="Python executable for flash_attention_2 env.",
    )
    parser.add_argument(
        "--language",
        default="Russian",
        help="Language hint passed to ASR.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=1024,
        help="max_new_tokens for transcription.",
    )
    parser.add_argument(
        "--max-inference-batch-size",
        type=int,
        default=32,
        help="max_inference_batch_size for qwen-asr.",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"Audio file not found: {args.audio}", file=sys.stderr)
        return 2
    if not args.sdpa_python.exists():
        print(f"sdpa python not found: {args.sdpa_python}", file=sys.stderr)
        return 2
    if not args.fa2_python.exists():
        print(f"fa2 python not found: {args.fa2_python}", file=sys.stderr)
        return 2

    print(f"Audio: {args.audio}")
    print("Running sdpa baseline...")
    sdpa = _run_probe(
        python_exe=args.sdpa_python,
        audio=args.audio,
        attn="sdpa",
        max_new_tokens=args.max_new_tokens,
        max_inference_batch_size=args.max_inference_batch_size,
        language=args.language,
    )
    print("Running flash_attention_2...")
    fa2 = _run_probe(
        python_exe=args.fa2_python,
        audio=args.audio,
        attn="flash_attention_2",
        max_new_tokens=args.max_new_tokens,
        max_inference_batch_size=args.max_inference_batch_size,
        language=args.language,
    )

    infer_speedup = (
        (sdpa["infer_s"] / fa2["infer_s"])
        if fa2["infer_s"] and sdpa["infer_s"]
        else 0.0
    )

    print("\nResults:")
    print(
        json.dumps(
            {
                "sdpa": {
                    "python": str(args.sdpa_python),
                    "torch": sdpa["torch"],
                    "cuda": sdpa["cuda"],
                    "load_s": sdpa["load_s"],
                    "infer_s": sdpa["infer_s"],
                    "total_s": sdpa["total_s"],
                    "chars": sdpa["chars"],
                },
                "flash_attention_2": {
                    "python": str(args.fa2_python),
                    "torch": fa2["torch"],
                    "cuda": fa2["cuda"],
                    "load_s": fa2["load_s"],
                    "infer_s": fa2["infer_s"],
                    "total_s": fa2["total_s"],
                    "chars": fa2["chars"],
                },
                "infer_speedup_x": round(infer_speedup, 3),
                "same_char_count": sdpa["chars"] == fa2["chars"],
                "same_preview": sdpa["preview"] == fa2["preview"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
