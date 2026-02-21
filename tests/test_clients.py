from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from chronicle_keeper.llm_client import LLMClient
from chronicle_keeper.whisper_client import WhisperClient


def _make_settings(
    port: int, api_style: str = "asr", asr_path: str = "/asr"
) -> SimpleNamespace:
    return SimpleNamespace(
        whisper_base_url=f"http://127.0.0.1:{port}",
        whisper_api_style=api_style,
        whisper_asr_path=asr_path,
        whisper_openai_model="openai/whisper-large-v3-turbo",
        whisper_openai_temperature=0.0,
        whisper_openai_prompt="",
        whisper_language="ru",
        whisper_task="transcribe",
        whisper_encode=True,
        whisper_warmup_on_start=False,
        whisper_fallback_enabled=False,
        whisper_fallback_base_url="",
        whisper_fallback_api_style=api_style,
        whisper_fallback_asr_path=asr_path,
        whisper_fallback_openai_model="openai/whisper-large-v3-turbo",
        whisper_fallback_on_low_quality=False,
        whisper_low_quality_min_chars=40,
        whisper_low_quality_min_segments=1,
        llm_base_url=f"http://127.0.0.1:{port}/v1",
        llm_model="stub-model",
        llm_temperature=0.0,
        llm_max_tokens=256,
        llm_warmup_on_start=False,
    )


async def _run_server(app: web.Application) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets  # type: ignore[attr-defined]
    assert sockets
    port = sockets[0].getsockname()[1]
    return runner, port


def test_whisper_uses_segment_fallback(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")

    async def _run() -> None:
        async def asr_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {"segments": [{"text": "hello"}, {"text": "world"}]}
            )

        app = web.Application()
        app.router.add_post("/asr", asr_handler)
        runner, port = await _run_server(app)
        try:
            client = WhisperClient(_make_settings(port))
            text = await client.transcribe_file(audio)
            assert text == "hello world"
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_whisper_raises_on_http_error(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")

    async def _run() -> None:
        async def asr_handler(request: web.Request) -> web.Response:
            return web.Response(status=500, text="boom")

        app = web.Application()
        app.router.add_post("/asr", asr_handler)
        runner, port = await _run_server(app)
        try:
            client = WhisperClient(_make_settings(port))
            try:
                await client.transcribe_file(audio)
                assert False, "Expected RuntimeError for HTTP 500"
            except RuntimeError as exc:
                assert "Whisper error 500" in str(exc)
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_whisper_openai_transcriptions_mode(tmp_path: Path) -> None:
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3fake")

    async def _run() -> None:
        async def asr_handler(request: web.Request) -> web.Response:
            data = await request.post()
            assert "file" in data
            assert data.get("model") == "openai/whisper-large-v3-turbo"
            return web.json_response(
                {
                    "text": "hello world",
                    "segments": [
                        {"start": 0.0, "end": 0.5, "text": "hello"},
                        {"start": 0.6, "end": 1.1, "text": "world"},
                    ],
                }
            )

        app = web.Application()
        app.router.add_post("/v1/audio/transcriptions", asr_handler)
        runner, port = await _run_server(app)
        try:
            client = WhisperClient(
                _make_settings(
                    port,
                    api_style="openai",
                    asr_path="/v1/audio/transcriptions",
                )
            )
            detailed = await client.transcribe_file_detailed(audio)
            assert detailed.text == "hello world"
            assert len(detailed.segments) == 2
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_whisper_fallback_when_primary_unreachable(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")

    async def _run() -> None:
        async def asr_handler(request: web.Request) -> web.Response:
            return web.json_response({"text": "fallback ok"})

        app = web.Application()
        app.router.add_post("/asr", asr_handler)
        runner, port = await _run_server(app)
        try:
            settings = _make_settings(65500, api_style="asr", asr_path="/asr")
            settings.whisper_fallback_enabled = True
            settings.whisper_fallback_base_url = f"http://127.0.0.1:{port}"
            settings.whisper_fallback_api_style = "asr"
            settings.whisper_fallback_asr_path = "/asr"
            client = WhisperClient(settings)
            text = await client.transcribe_file(audio)
            assert text == "fallback ok"
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_whisper_fallback_on_low_quality_result(tmp_path: Path) -> None:
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFFfake")

    async def _run() -> None:
        async def primary_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {"text": "ok", "segments": [{"start": 0.0, "end": 0.2, "text": "ok"}]}
            )

        async def fallback_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "text": "this fallback transcript is clearly richer",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "fallback richer"}],
                }
            )

        app_primary = web.Application()
        app_primary.router.add_post("/asr", primary_handler)
        app_fallback = web.Application()
        app_fallback.router.add_post("/asr", fallback_handler)

        runner_primary, port_primary = await _run_server(app_primary)
        runner_fallback, port_fallback = await _run_server(app_fallback)
        try:
            settings = _make_settings(port_primary, api_style="asr", asr_path="/asr")
            settings.whisper_fallback_enabled = True
            settings.whisper_fallback_base_url = f"http://127.0.0.1:{port_fallback}"
            settings.whisper_fallback_api_style = "asr"
            settings.whisper_fallback_asr_path = "/asr"
            settings.whisper_fallback_on_low_quality = True
            settings.whisper_low_quality_min_chars = 10
            settings.whisper_low_quality_min_segments = 1
            client = WhisperClient(settings)
            text = await client.transcribe_file(audio)
            assert "fallback richer" in text
        finally:
            await runner_primary.cleanup()
            await runner_fallback.cleanup()

    asyncio.run(_run())


def test_llm_raises_on_http_error() -> None:
    async def _run() -> None:
        async def llm_handler(request: web.Request) -> web.Response:
            return web.json_response({"error": "unavailable"}, status=503)

        app = web.Application()
        app.router.add_post("/v1/chat/completions", llm_handler)
        runner, port = await _run_server(app)
        try:
            client = LLMClient(_make_settings(port))
            try:
                await client.generate_summary("hello", language="ru")
                assert False, "Expected RuntimeError for HTTP 503"
            except RuntimeError as exc:
                assert "LLM error 503" in str(exc)
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_llm_raises_on_unexpected_shape() -> None:
    async def _run() -> None:
        async def llm_handler(request: web.Request) -> web.Response:
            return web.json_response({"not_choices": []})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", llm_handler)
        runner, port = await _run_server(app)
        try:
            client = LLMClient(_make_settings(port))
            try:
                await client.generate_summary("hello", language="ru")
                assert False, "Expected RuntimeError for malformed response"
            except RuntimeError as exc:
                assert "Unexpected LLM response" in str(exc)
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_llm_summary_normalizes_missing_sections() -> None:
    async def _run() -> None:
        async def llm_handler(request: web.Request) -> web.Response:
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "Only one loose paragraph without headers."
                            }
                        }
                    ]
                }
            )

        app = web.Application()
        app.router.add_post("/v1/chat/completions", llm_handler)
        runner, port = await _run_server(app)
        try:
            client = LLMClient(_make_settings(port))
            summary = await client.generate_summary("hello", language="ru")
            for header in (
                "# Session Summary",
                "# Key Events",
                "# NPCs and Factions",
                "# Open Threads",
                "# Player-Facing Chronicle Post",
            ):
                assert header in summary
        finally:
            await runner.cleanup()

    asyncio.run(_run())


def test_llm_warmup_enabled() -> None:
    async def _run() -> None:
        async def llm_handler(request: web.Request) -> web.Response:
            payload = await request.json()
            assert payload.get("messages")
            return web.json_response({"choices": [{"message": {"content": "OK"}}]})

        app = web.Application()
        app.router.add_post("/v1/chat/completions", llm_handler)
        runner, port = await _run_server(app)
        try:
            settings = _make_settings(port)
            settings.llm_warmup_on_start = True
            client = LLMClient(settings)
            ok, details = await client.warmup()
            assert ok is True
            assert details == "ok"
        finally:
            await runner.cleanup()

    asyncio.run(_run())
