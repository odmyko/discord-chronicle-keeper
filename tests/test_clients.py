from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from aiohttp import web

from chronicle_keeper.llm_client import LLMClient
from chronicle_keeper.whisper_client import WhisperClient


def _make_settings(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        whisper_base_url=f"http://127.0.0.1:{port}",
        whisper_asr_path="/asr",
        whisper_language="ru",
        whisper_task="transcribe",
        whisper_encode=True,
        llm_base_url=f"http://127.0.0.1:{port}/v1",
        llm_model="stub-model",
        llm_temperature=0.0,
        llm_max_tokens=256,
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
            return web.json_response({"segments": [{"text": "hello"}, {"text": "world"}]})

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
