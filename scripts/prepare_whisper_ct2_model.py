from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import re
import shutil
import subprocess
import sys


def _safe_name(value: str) -> str:
    value = value.strip().replace("\\", "/")
    value = value.split("/")[-1]
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return value or "whisper-model"


def _set_env_value(lines: list[str], key: str, value: str) -> list[str]:
    new_line = f"{key}={value}"
    updated = False
    out: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(new_line)
            updated = True
        else:
            out.append(line)
    if not updated:
        out.append(new_line)
    return out


def update_env_file(env_path: Path, model_dir_name: str) -> None:
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    lines = _set_env_value(lines, "WHISPER_ASR_ENGINE", "faster_whisper")
    lines = _set_env_value(lines, "WHISPER_ASR_MODEL_PATH", "/models/whisper")
    lines = _set_env_value(lines, "WHISPER_ASR_MODEL", f"/models/whisper/{model_dir_name}")

    env_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download/convert HF Whisper model to CTranslate2 and wire .env for docker-compose.",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Hugging Face model id, e.g. anuragshas/whisper-large-v2-uk",
    )
    parser.add_argument(
        "--quantization",
        default="float16",
        help="CTranslate2 quantization (default: float16)",
    )
    parser.add_argument(
        "--output-name",
        default="",
        help="Target folder name under data/whisper-models (default: derived from model id)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to env file to update (default: .env)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing output directory if it exists.",
    )
    args = parser.parse_args()

    output_name = args.output_name.strip() or _safe_name(args.model)
    output_dir = Path("data") / "whisper-models" / output_name
    env_path = Path(args.env_file)

    missing_modules = [
        name
        for name in ("ctranslate2", "transformers", "torch")
        if importlib.util.find_spec(name) is None
    ]
    if missing_modules:
        print(
            "Error: missing Python dependencies in current interpreter "
            f"({sys.executable}): {', '.join(missing_modules)}"
        )
        print("Install with:")
        print(f'  "{sys.executable}" -m pip install ctranslate2 transformers torch')
        return 1

    if output_dir.exists():
        if not args.force:
            print(f"Output already exists: {output_dir}")
            print("Use --force to overwrite or choose a different --output-name.")
            return 1
        shutil.rmtree(output_dir)

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "ctranslate2.converters.transformers",
        "--model",
        args.model,
        "--output_dir",
        str(output_dir),
        "--quantization",
        args.quantization,
        "--copy_files",
        "tokenizer.json",
        "preprocessor_config.json",
    ]

    print("Running:", " ".join(cmd))
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        print("Error: Python executable not found.")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"Error: model conversion failed with exit code {exc.returncode}.")
        return exc.returncode

    required_files = [
        output_dir / "model.bin",
        output_dir / "tokenizer.json",
        output_dir / "preprocessor_config.json",
    ]
    missing_files = [str(p) for p in required_files if not p.exists()]
    if missing_files:
        print("Error: conversion finished but required files are missing:")
        for path in missing_files:
            print(f"  - {path}")
        print("Try running again with --force.")
        return 1

    update_env_file(env_path, output_name)
    print(f"Model converted to: {output_dir}")
    print(f"Updated env file: {env_path}")
    print("Set values:")
    print("  WHISPER_ASR_ENGINE=faster_whisper")
    print("  WHISPER_ASR_MODEL_PATH=/models/whisper")
    print(f"  WHISPER_ASR_MODEL=/models/whisper/{output_name}")
    print("Next: docker compose up -d --build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
