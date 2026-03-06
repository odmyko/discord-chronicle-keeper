from pathlib import Path

from chronicle_keeper.processor import (
    SessionProcessor,
    SpeakerTranscript,
    sanitize_name,
)


def test_sanitize_name():
    assert sanitize_name(" John Galt ") == "John_Galt"
    assert sanitize_name("РРјСЏ!?") == "unknown"


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


def test_clean_transcript_text_removes_common_hallucinations():
    source = "DimaTorzok Игрок открывает дверь и зовет остальных."

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert "DimaTorzok" not in cleaned
    assert "Игрок открывает дверь" in cleaned


def test_clean_transcript_text_removes_standalone_torzok_noise():
    source = "DimaTorzok Dima Torzok Игроки вскрывают дверь и спорят с дуэргарами."

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert "DimaTorzok" not in cleaned
    assert "Dima Torzok" not in cleaned
    assert "Игроки вскрывают дверь" in cleaned


def test_clean_transcript_text_collapses_repeated_short_phrases():
    source = "РЎРїР°СЃРёР±Рѕ. РЎРїР°СЃРёР±Рѕ. РЎРїР°СЃРёР±Рѕ. РЎРїР°СЃРёР±Рѕ. РћРєРµР№. Рё Рё Рё Рё Рё"

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert cleaned == "РЎРїР°СЃРёР±Рѕ. РћРєРµР№. Рё"


def test_clean_transcript_text_keeps_double_repeats():
    source = "Р”Р°. Р”Р°. РџРѕС‚РѕРј РёРґРµРј РґР°Р»СЊС€Рµ."

    cleaned = SessionProcessor._clean_transcript_text(source)

    assert cleaned == "Р”Р°. Р”Р°. РџРѕС‚РѕРј РёРґРµРј РґР°Р»СЊС€Рµ."


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
