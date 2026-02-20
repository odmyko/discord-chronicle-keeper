from __future__ import annotations

import fnmatch
from pathlib import PurePosixPath
import subprocess
import sys


MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024  # 5 MiB


FORBIDDEN_PATTERNS = [
    "data/**",
    ".env",
    ".env.*",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/.pytest_cache/**",
    "**/*.wav",
    "**/*.mp3",
    "**/*.flac",
    "**/*.m4a",
    "**/*.ogg",
    "**/*.opus",
    "**/processing_state.json",
    "**/full_transcript.txt",
    "**/full_transcript.md",
    "**/summary.md",
    "**/chunk_summaries.md",
]


ALLOWED_EXACT = {
    ".env.example",
}


def _git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_forbidden(path: str) -> bool:
    if path in ALLOWED_EXACT:
        return False
    posix_path = str(PurePosixPath(path))
    return any(fnmatch.fnmatch(posix_path, pattern) for pattern in FORBIDDEN_PATTERNS)


def main() -> int:
    tracked = _git_lines("ls-files")
    if not tracked:
        print("[repo-hygiene] no tracked files found")
        return 0

    forbidden_hits = [path for path in tracked if _is_forbidden(path)]

    query = "\n".join(f"HEAD:{path}" for path in tracked) + "\n"
    batch = subprocess.run(
        ["git", "cat-file", "--batch-check=%(objectsize) %(rest)"],
        check=True,
        capture_output=True,
        text=True,
        input=query,
    )
    oversized_hits: list[tuple[str, int]] = []
    for line in batch.stdout.splitlines():
        size_str, path = line.split(" ", 1)
        size = int(size_str)
        if size > MAX_FILE_SIZE_BYTES:
            oversized_hits.append((path, size))

    if not forbidden_hits and not oversized_hits:
        print("[repo-hygiene] ok")
        return 0

    print("[repo-hygiene] failed")
    if forbidden_hits:
        print("Forbidden tracked files:")
        for path in forbidden_hits:
            print(f"  - {path}")
    if oversized_hits:
        print(f"Tracked files larger than {MAX_FILE_SIZE_BYTES} bytes:")
        for path, size in oversized_hits:
            print(f"  - {path} ({size} bytes)")
    print(
        "If this file is intentional, move it outside the repo or adjust "
        "scripts/check_repo_hygiene.py policy."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
