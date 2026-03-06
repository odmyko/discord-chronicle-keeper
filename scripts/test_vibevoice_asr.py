from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def _normalize_language(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    mapping = {
        "ru": "Russian",
        "en": "English",
        "uk": "Ukrainian",
        "ua": "Ukrainian",
    }
    return mapping.get(normalized.lower(), normalized)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run VibeVoice-ASR on one local audio file"
    )
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--model", default="microsoft/VibeVoice-ASR-HF")
    parser.add_argument(
        "--dtype", choices=["auto", "bfloat16", "float16", "float32"], default="float16"
    )
    parser.add_argument("--language", default=None)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON payload only"
    )
    args = parser.parse_args()

    if not args.audio.exists():
        raise SystemExit(f"Audio file not found: {args.audio}")

    os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")
    os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")

    import torch
    from transformers import AutoProcessor, VibeVoiceAsrForConditionalGeneration

    dtype_map = {
        "bfloat16": torch.bfloat16,
        "float16": torch.float16,
        "float32": torch.float32,
    }
    dtype = dtype_map.get(
        args.dtype, torch.float16 if torch.cuda.is_available() else torch.float32
    )

    t0 = time.perf_counter()
    processor = AutoProcessor.from_pretrained(args.model)
    model = VibeVoiceAsrForConditionalGeneration.from_pretrained(
        args.model,
        torch_dtype=dtype,
        device_map="cuda:0" if torch.cuda.is_available() else "cpu",
    )

    request_kwargs: dict[str, object] = {"audio": str(args.audio)}
    language = _normalize_language(args.language)
    if language:
        request_kwargs["language"] = language

    inputs = processor.apply_transcription_request(**request_kwargs).to(
        model.device, model.dtype
    )
    generated = model.generate(**inputs, max_new_tokens=max(256, args.max_new_tokens))
    generated_ids = generated[:, inputs["input_ids"].shape[1] :]

    parsed = []
    text = ""
    try:
        parsed = processor.decode(generated_ids, return_format="parsed")[0]
        text = processor.decode(generated_ids, return_format="transcription_only")[0]
    except Exception:
        # fallback when model emitted malformed structured json
        raw = processor.decode(generated_ids, return_format="raw")[0]
        text = str(raw)

    t1 = time.perf_counter()

    segments: list[dict[str, object]] = []
    for row in parsed or []:
        start = float(row.get("Start", row.get("start", 0.0)))
        end = float(row.get("End", row.get("end", 0.0)))
        seg_text = str(row.get("Content", row.get("text", "")) or "").strip()
        speaker = row.get("Speaker", row.get("speaker", None))
        if seg_text:
            segments.append(
                {"start": start, "end": end, "speaker": speaker, "text": seg_text}
            )

    payload = {
        "model": args.model,
        "audio": str(args.audio),
        "language_hint": language,
        "elapsed_s": round(t1 - t0, 3),
        "text": str(text or "").strip(),
        "segments": segments,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(f"model={payload['model']}")
        print(f"audio={payload['audio']}")
        print(f"elapsed_s={payload['elapsed_s']}")
        if payload["segments"]:
            print(f"segments={len(payload['segments'])}")
        print("text:")
        print(payload["text"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
