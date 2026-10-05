"""자막 분할 / Whisper 타이밍 정렬."""

from __future__ import annotations

from app.schemas import Scene, SceneTiming
from app.subtitle.chunker import build_subtitles, split_chunks, to_srt
from app.subtitle.whisper import Word
from app.video.subtitle_renderer import is_highlight


def test_split_chunks_short_and_sentence_aware():
    chunks = split_chunks("이 배는 갑자기 흔적도 없이 사라졌습니다. 그런데 이상한 건 지금부터입니다.", max_chars=8, max_words=3)
    assert all(len(c.replace(" ", "")) <= 12 for c in chunks)
    assert "사라졌습니다" in chunks  # 문장 끝에서 끊고 마침표 제거
    assert chunks[-1] == "지금부터입니다"


def _timing(n, start, end, lead=0.0):
    return SceneTiming(scene_number=n, start=start, end=end, speech_start=start + lead, speech_end=end - 0.2, audio_path="")


def test_alignment_uses_whisper_timing():
    scenes = [Scene(scene_number=1, narration="이 배는 갑자기 사라졌습니다.", visual_search_query="q", emphasis_words=["사라졌습니다"])]
    words = [Word(0.2, 0.5, "이"), Word(0.5, 0.9, "배는"), Word(1.5, 2.0, "갑자기"), Word(2.2, 3.0, "사라졌습니다.")]
    chunks = build_subtitles(scenes, [_timing(1, 0.0, 3.3, 0.2)], words, max_chars=4, max_words=2)
    assert chunks[0].start <= 0.3
    last = chunks[-1]
    assert last.text == "사라졌습니다" and 2.0 <= last.start <= 2.3 and last.highlights == ["사라졌습니다"]
    assert all(a.end <= b.start + 1e-6 for a, b in zip(chunks, chunks[1:]))  # 겹침 없음


def test_alignment_falls_back_when_whisper_missed_words():
    scenes = [
        Scene(scene_number=1, narration="첫 장면의 문장입니다.", visual_search_query="q"),
        Scene(scene_number=2, narration="두 번째 장면입니다.", visual_search_query="q"),
    ]
    timings = [_timing(1, 0.0, 3.0, 0.2), _timing(2, 3.0, 6.0)]
    words = [Word(3.1, 3.5, "두"), Word(3.5, 4.0, "번째"), Word(4.0, 5.5, "장면입니다.")]  # 1번 Scene 인식 누락
    chunks = build_subtitles(scenes, timings, words, max_chars=6, max_words=2)
    first = [c for c in chunks if c.scene_number == 1]
    assert first and first[0].start < 1.0 and first[-1].end <= 3.1


def test_srt_format():
    scenes = [Scene(scene_number=1, narration="안녕하세요.", visual_search_query="q")]
    srt = to_srt(build_subtitles(scenes, [_timing(1, 0, 2)], [], max_chars=10))
    assert srt.startswith("1\n00:00:00,000 --> 00:00:0")


def test_highlight_single_char_exact_only():
    assert not is_highlight("한가운데서", {"한"})
    assert is_highlight("한", {"한"})
    assert is_highlight("구명보트뿐이었습니다.", {"구명보트"})
