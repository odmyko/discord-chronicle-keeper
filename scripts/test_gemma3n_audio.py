import argparse
import os
from pathlib import Path

os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a quick Gemma 3n audio test with Hugging Face Transformers."
    )
    parser.add_argument(
        "--audio",
        required=True,
        help="Path to local audio file (mp3, wav, m4a, ogg).",
    )
    parser.add_argument(
        "--model",
        default="google/gemma-3n-e4b-it",
        help="Transformers model id. Default: google/gemma-3n-e4b-it",
    )
    parser.add_argument(
        "--prompt",
        default="Transcribe this audio in Russian. Return only the transcription.",
        help="User text prompt sent together with the audio.",
    )
    parser.add_argument(
        "--device",
        default="auto",
        help="Device map passed to transformers pipeline. Default: auto",
    )
    parser.add_argument(
        "--dtype",
        default="auto",
        choices=["auto", "float16", "bfloat16", "float32"],
        help="Torch dtype for model loading.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=512,
        help="Max new tokens to generate. Default: 512",
    )
    parser.add_argument(
        "--hf-token",
        default="",
        help="Hugging Face token for gated Gemma repos. Defaults to HF_TOKEN/HUGGINGFACE_HUB_TOKEN env vars.",
    )
    return parser


def resolve_torch_dtype(name: str):
    import torch

    if name == "float16":
        return torch.float16
    if name == "bfloat16":
        return torch.bfloat16
    if name == "float32":
        return torch.float32
    return "auto"


def main() -> int:
    args = build_parser().parse_args()
    audio_path = Path(args.audio)
    if not audio_path.is_file():
        raise SystemExit(f"Audio file not found: {audio_path}")

    hf_token = (
        args.hf_token.strip()
        or os.getenv("HF_TOKEN", "").strip()
        or os.getenv("HUGGINGFACE_HUB_TOKEN", "").strip()
        or os.getenv("HUGGING_FACE_HUB_TOKEN", "").strip()
    )

    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    torch.set_float32_matmul_precision("high")
    dtype = resolve_torch_dtype(args.dtype)
    try:
        processor = AutoProcessor.from_pretrained(
            args.model,
            token=hf_token or None,
        )
        model = AutoModelForImageTextToText.from_pretrained(
            args.model,
            device_map=args.device,
            torch_dtype=dtype,
            token=hf_token or None,
        )
    except OSError as exc:
        message = str(exc)
        if "gated repo" in message.lower() or "401" in message:
            raise SystemExit(
                "Model download failed: this Gemma repo is gated on Hugging Face.\n"
                "1. Request access to the model on huggingface.co\n"
                "2. Create a HF token\n"
                "3. Run with --hf-token <token> or set HF_TOKEN/HUGGINGFACE_HUB_TOKEN"
            ) from exc
        raise

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "audio", "audio": audio_path.as_posix()},
                {"type": "text", "text": args.prompt},
            ],
        }
    ]

    inputs = processor.apply_chat_template(
        messages,
        add_generation_prompt=True,
        tokenize=True,
        return_dict=True,
        return_tensors="pt",
    )
    normalized_inputs = {}
    for key, value in inputs.items():
        if hasattr(value, "is_floating_point") and value.is_floating_point():
            normalized_inputs[key] = value.to(device=model.device, dtype=model.dtype)
        else:
            normalized_inputs[key] = value.to(model.device)
    inputs = normalized_inputs
    outputs = model.generate(**inputs, max_new_tokens=args.max_new_tokens)
    prompt_length = inputs["input_ids"].shape[1]
    generated_tokens = outputs[:, prompt_length:]
    text = processor.batch_decode(
        generated_tokens,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )

    print(f"model={args.model}")
    print(f"audio={audio_path}")
    print(f"cuda_available={torch.cuda.is_available()}")
    print("response:")
    print(text[0].strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
