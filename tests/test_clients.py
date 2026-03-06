from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys

from aiohttp import web

from chronicle_keeper.asr import create_asr_client
from chronicle_keeper.llm_client import LLMClient


def _make_settings(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        asr_backend="qwen3_asr",
        asr_language="ru",
        qwen_asr_backend="transformers",
        qwen_asr_model="Qwen/Qwen3-ASR-1.7B",
        qwen_asr_dtype="auto",
        qwen_asr_attn_implementation="auto",
        qwen_asr_max_new_tokens=4096,
        qwen_asr_max_inference_batch_size=32,
        qwen_asr_gpu_memory_utilization=0.8,
        qwen_asr_warmup_on_start=False,
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


def test_create_asr_client_returns_qwen_client(monkeypatch) -> None:
    fake_module = SimpleNamespace()

    class FakeResult:
        language = "Russian"
        text = "hello from qwen"

    class FakeModel:
        def transcribe(
            self, audio: str, language: str | None, return_time_stamps: bool
        ):
            assert language == "Russian"
            assert return_time_stamps is False
            return [FakeResult()]

    class FakeQwen3ASRModel:
        @staticmethod
        def from_pretrained(model: str, **kwargs):
            assert model == "Qwen/Qwen3-ASR-1.7B"
            assert kwargs["max_new_tokens"] == 4096
            return FakeModel()

    fake_module.Qwen3ASRModel = FakeQwen3ASRModel
    monkeypatch.setitem(sys.modules, "qwen_asr", fake_module)

    async def _run() -> None:
        settings = _make_settings(12345)
        client = create_asr_client(settings)
        detailed = await client.transcribe_file_detailed(Path("dummy.wav"))
        assert detailed.text == "hello from qwen"
        assert detailed.segments == []

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
