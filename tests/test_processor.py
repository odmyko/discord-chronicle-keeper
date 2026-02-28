from pathlib import Path

from chronicle_keeper.processor import (
    SessionProcessor,
    SpeakerTranscript,
    sanitize_name,
)
from chronicle_keeper.whisper_client import TranscriptSegment


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
    result = SessionProcessor._build_transcript_text(items, [])
    assert "alice (1)" in result
    assert "bob (2)" in result


def test_clean_transcript_text_removes_common_whisper_hallucinations():
    source = (
        "Субтитры создавал DimaTorzok Продолжение следует... "
        "Редактор субтитров А.Семкин Корректор А.Кулакова "
        "Игрок открывает дверь и зовет остальных."
    )

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert "DimaTorzok" not in cleaned
    assert "Продолжение следует" not in cleaned
    assert "Корректор" not in cleaned
    assert "Игрок открывает дверь" in cleaned


def test_clean_transcript_text_collapses_repeated_short_phrases():
    source = "Спасибо. Спасибо. Спасибо. Спасибо. Окей. и и и и и"

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert cleaned == "Спасибо. Окей. и"


def test_timeline_entries_from_segments_drop_cleaned_noise():
    segments = [
        TranscriptSegment(start=0.0, end=1.0, text="Продолжение следует..."),
        TranscriptSegment(start=1.0, end=2.0, text="Игрок наносит удар."),
    ]
    cleaned_segments = [
        TranscriptSegment(start=s.start, end=s.end, text=cleaned)
        for s in segments
        if (cleaned := SessionProcessor._clean_transcript_text(s.text))
    ]

    entries = SessionProcessor._timeline_entries_from_segments(
        cleaned_segments,
        segment_index=1,
        user_id=1,
        speaker_name="alice",
    )

    assert len(entries) == 1
    assert entries[0].text == "Игрок наносит удар."


def test_parse_saved_audio_filename():
    parsed = SessionProcessor._parse_saved_audio_filename(
        Path("johngalt_451102877562306570_seg002.mp3")
    )
    assert parsed is not None
    assert parsed.speaker_name == "johngalt"
    assert parsed.user_id == 451102877562306570
    assert parsed.segment_index == 2


def test_collect_saved_audio_entries_with_fallback(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / "alice_1_seg002.mp3").write_bytes(b"x")
    (audio_dir / "alice_1_seg001.mp3").write_bytes(b"x")
    (audio_dir / "bad-name.wav").write_bytes(b"x")

    entries = SessionProcessor._collect_saved_audio_entries(audio_dir)
    assert len(entries) == 3
    assert entries[0].user_id == 0
    assert entries[0].speaker_name == "unknown"
    assert entries[1].segment_index == 1
    assert entries[2].segment_index == 2
