from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys


KEYS_BY_BACKEND = {
    "asr": {
        "BOT_WHISPER_BASE_URL": "http://whisper:9000",
        "WHISPER_API_STYLE": "asr",
        "WHISPER_ASR_PATH": "/asr",
    },
    "vllm": {
        "BOT_WHISPER_BASE_URL": "http://whisper_vllm:8000",
        "WHISPER_API_STYLE": "openai",
        "WHISPER_ASR_PATH": "/v1/audio/transcriptions",
    },
}


def _set_env_value(env_text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"^(?P<k>{re.escape(key)})=.*$", re.MULTILINE)
    if pattern.search(env_text):
        return pattern.sub(f"{key}={value}", env_text)
    if env_text and not env_text.endswith("\n"):
        env_text += "\n"
    return env_text + f"{key}={value}\n"


def _run_command(args: list[str]) -> None:
    proc = subprocess.run(args, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(args)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch Chronicle Keeper ASR backend profile and env wiring.")
    parser.add_argument("--backend", choices=["asr", "vllm"], required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--up", action="store_true", help="Also run docker compose stop+up for selected backend.")
    args = parser.parse_args()

    env_file = args.env_file
    if not env_file.exists():
        print(f"Error: env file not found: {env_file}")
        return 2

    env_text = env_file.read_text(encoding="utf-8")
    for key, value in KEYS_BY_BACKEND[args.backend].items():
        env_text = _set_env_value(env_text, key, value)
    env_file.write_text(env_text, encoding="utf-8")
    print(f"Updated {env_file} for backend={args.backend}")

    if args.up:
        _run_command(["docker", "compose", "stop", "whisper", "whisper_vllm", "bot"])
        _run_command(["docker", "compose", "--profile", args.backend, "up", "-d", "--build"])
        print(f"Docker compose switched to profile={args.backend}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"Error: {exc}")
        raise SystemExit(1)

