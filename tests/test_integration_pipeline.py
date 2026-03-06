from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import wave

from aiohttp import web

from chronicle_keeper.asr import TranscriptResult
from chronicle_keeper.llm_client import LLMClient
from chronicle_keeper.processor import SessionProcessor


def _build_wav_bytes(duration_seconds: float = 0.2, sample_rate: int = 16000) -> bytes:
    frames = int(duration_seconds * sample_rate)
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class _FakeAudioData:
    def __init__(self, payload: bytes) -> None:
        self.file = BytesIO(payload)


class _FakeSink:
    def __init__(self, audio_data: dict[str, _FakeAudioData]) -> None:
        self.audio_data = audio_data


class _FakeGuild:
    def __init__(self, guild_id: int, members: dict[int, str]) -> None:
        self.id = guild_id
        self._members = members

    def get_member(self, user_id: int):
        name = self._members.get(user_id)
        if name is None:
            return None
        return SimpleNamespace(display_name=name)


class _FakeASRClient:
    async def transcribe_file(self, audio_path: Path) -> str:
        return "privet mir"

    async def transcribe_file_detailed(self, audio_path: Path) -> TranscriptResult:
        return TranscriptResult(text="privet mir", segments=[])

    async def warmup(self) -> tuple[bool, str]:
        return True, "ok"


async def _run_pipeline(tmp_path: Path) -> None:
    async def llm_handler(request: web.Request) -> web.Response:
        payload = await request.json()
        assert payload.get("messages")
        return web.json_response(
            {"choices": [{"message": {"content": "# Session Summary\nok"}}]}
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", llm_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[attr-defined]
    assert sockets
    port = sockets[0].getsockname()[1]

    settings = SimpleNamespace(
        llm_base_url=f"http://127.0.0.1:{port}/v1",
        llm_model="stub-model",
        llm_temperature=0.0,
        llm_max_tokens=256,
        llm_warmup_on_start=False,
    )

    asr = _FakeASRClient()
    llm = LLMClient(settings)
    processor = SessionProcessor(tmp_path, asr, llm)

    sink = _FakeSink({"123": _FakeAudioData(_build_wav_bytes())})
    guild = _FakeGuild(42, {123: "johngalt"})

    try:
        artifacts = await processor.process_sinks(guild, [sink], summary_language="ru")
    finally:
        await runner.cleanup()

    assert artifacts.session_dir.exists()
    assert artifacts.full_transcript_txt_path.exists()
    assert artifacts.summary_path.exists()
    assert "johngalt (123)" in artifacts.full_transcript_txt_path.read_text(
        encoding="utf-8"
    )
    assert "# Session Summary" in artifacts.summary_markdown


def test_processing_pipeline_with_stub_http_services(tmp_path: Path) -> None:
    asyncio.run(_run_pipeline(tmp_path))
