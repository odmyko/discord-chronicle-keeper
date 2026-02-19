from pathlib import Path

from chronicle_keeper.processor import SessionProcessor, SpeakerTranscript, sanitize_name


def test_sanitize_name():
    assert sanitize_name(" John Galt ") == "John_Galt"
    assert sanitize_name("Имя!?") == "unknown"


def test_split_transcript_for_summary():
    text = "line1\n" * 500
    chunks = SessionProcessor._split_transcript_for_summary(text, max_chars=200)
    assert len(chunks) > 1
    assert all(len(c) <= 210 for c in chunks)


def test_build_transcript_text():
    items = [
        SpeakerTranscript(
            user_id=1,
            speaker_name="alice",
            audio_path=Path("."),
            transcript="hello",
        ),
        SpeakerTranscript(
            user_id=2,
            speaker_name="bob",
            audio_path=Path("."),
            transcript="world",
        ),
    ]
    result = SessionProcessor._build_transcript_text(items)
    assert "alice (1)" in result
    assert "bob (2)" in result
