from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length > 0:
            _ = self.rfile.read(length)

        if self.path.startswith("/asr") or self.path.startswith(
            "/v1/audio/transcriptions"
        ):
            payload = {
                "text": "Smoke transcript from mock ASR.",
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "text": "Smoke transcript from mock ASR.",
                    },
                ],
            }
            return self._write_json(200, payload)

        if self.path.startswith("/chat/completions"):
            payload = {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "# Session Summary\nSmoke summary.\n\n"
                                "# Key Events\n- Event.\n\n"
                                "# NPCs and Factions\n- None.\n\n"
                                "# Open Threads\n- None.\n\n"
                                "# Player-Facing Chronicle Post\nChronicle post."
                            )
                        }
                    }
                ]
            }
            return self._write_json(200, payload)

        self._write_json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._write_json(200, {"ok": True})
            return
        self._write_json(404, {"error": "not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _write_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    server = HTTPServer(("0.0.0.0", 18080), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
