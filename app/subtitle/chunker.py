"""Shorts 스타일 짧은 자막 분할 + Whisper 타이밍 정렬.

text_source = "script": 대본 텍스트(오탈자 없음)를 짧게 나누고, Whisper 단어 타이밍에 글자 위치 비율로 맞춘다.
text_source = "whisper": Whisper 인식 단어를 그대로 묶는다.
"""

from __future__ import annotations

import re

from app.schemas import Scene, SceneTiming, SubtitleChunk
from app.subtitle.whisper import Word

_STRIP = re.compile(r"[\s\.,!?…·~\-\"'“”‘’()\[\]:;]+")
_SENTENCE_END = re.compile(r"[.!?…]$")


def _clen(text: str) -> int:
    return len(_STRIP.sub("", text))


def split_chunks(text: str, max_chars: int = 11, max_words: int = 3) -> list[str]:
    """어절 단위로 짧게 묶는다. 문장 끝/쉼표에서는 반드시 끊는다."""
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if current and (len(candidate.replace(" ", "")) > max_chars or len(current) >= max_words):
            chunks.append(" ".join(current))
            current = []
        current.append(word)
        if _SENTENCE_END.search(word) or word.endswith(","):
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    # 마침표/쉼표는 화면에서 제거 (물음표/느낌표는 유지)
    cleaned = [re.sub(r"[.,…]+$", "", c).strip() for c in chunks]
    return [c for c in cleaned if c]


def _time_at(position: float, timeline: list[tuple[int, int, float, float]]) -> float:
    """글자 위치(0~total)에 해당하는 시간. timeline = (char_start, char_end, t_start, t_end)."""
    for c0, c1, t0, t1 in timeline:
        if position <= c1:
            if c1 == c0:
                return t0
            frac = max(0.0, min(1.0, (position - c0) / (c1 - c0)))
            return t0 + (t1 - t0) * frac
    return timeline[-1][3]


def align_scene(
    scene: Scene,
    timing: SceneTiming,
    words: list[Word],
    max_chars: int,
    max_words: int,
    min_coverage: float = 0.6,
) -> list[tuple[float, float, str]]:
    chunks = split_chunks(scene.subtitle or scene.narration, max_chars, max_words)
    if not chunks:
        return []
    total_chars = sum(_clen(c) for c in chunks) or 1

    # Scene 구간(start~end)은 서로 겹치지 않으므로 단어 중앙 시간으로 하나의 Scene 에만 배정된다
    scene_words = [w for w in words if timing.start <= (w.start + w.end) / 2 < timing.end]
    timeline: list[tuple[int, int, float, float]] = []
    pos = 0
    for w in scene_words:
        n = max(_clen(w.text), 1)
        timeline.append((pos, pos + n, max(w.start, timing.speech_start - 0.1), min(w.end, timing.speech_end + 0.1)))
        pos += n
    whisper_chars = pos

    # 인식 누락이 많으면(커버리지 부족) 발화 구간에 글자 수 비율로 배분
    if not timeline or whisper_chars < total_chars * min_coverage:
        timeline = [(0, total_chars, timing.speech_start, timing.speech_end)]
        whisper_chars = total_chars

    out: list[tuple[float, float, str]] = []
    cursor = 0
    for chunk in chunks:
        n = _clen(chunk)
        start = _time_at(cursor / total_chars * whisper_chars, timeline)
        end = _time_at((cursor + n) / total_chars * whisper_chars, timeline)
        out.append((start, end, chunk))
        cursor += n
    return out


def group_whisper_words(words: list[Word], max_chars: int, max_words: int) -> list[tuple[float, float, str]]:
    out: list[tuple[float, float, str]] = []
    current: list[Word] = []
    for w in words:
        text = " ".join(x.text for x in current + [w])
        if current and (len(text.replace(" ", "")) > max_chars or len(current) >= max_words):
            out.append((current[0].start, current[-1].end, " ".join(x.text for x in current)))
            current = []
        current.append(w)
        if _SENTENCE_END.search(w.text):
            out.append((current[0].start, current[-1].end, " ".join(x.text for x in current)))
            current = []
    if current:
        out.append((current[0].start, current[-1].end, " ".join(x.text for x in current)))
    return [(s, e, re.sub(r"[.,…]+$", "", t)) for s, e, t in out]


def build_subtitles(
    scenes: list[Scene],
    timings: list[SceneTiming],
    words: list[Word],
    *,
    text_source: str = "script",
    max_chars: int = 11,
    max_words: int = 3,
    min_display: float = 0.35,
) -> list[SubtitleChunk]:
    timing_map = {t.scene_number: t for t in timings}
    raw: list[tuple[float, float, str, Scene]] = []
    if text_source == "whisper" and words:
        for start, end, text in group_whisper_words(words, max_chars, max_words):
            scene = next((s for s in scenes if timing_map[s.scene_number].start <= start < timing_map[s.scene_number].end), scenes[-1])
            raw.append((start, end, text, scene))
    else:
        for scene in scenes:
            for start, end, text in align_scene(scene, timing_map[scene.scene_number], words, max_chars, max_words):
                raw.append((start, end, text, scene))

    raw.sort(key=lambda x: x[0])
    chunks: list[SubtitleChunk] = []
    for i, (start, end, text, scene) in enumerate(raw):
        next_start = raw[i + 1][0] if i + 1 < len(raw) else end + 0.6
        # 다음 자막 직전까지 유지해서 깜빡임을 줄이되 너무 길게 남기지 않는다
        end = max(end, start + min_display)
        if next_start > start:
            end = min(end, next_start)
        if 0 <= next_start - end < 0.25:
            end = next_start
        words_in_chunk = text.split()
        highlights = [
            h for h in scene.emphasis_words
            if h and any(t == w.strip(".,!?") or (len(t) >= 2 and t in w) for t in h.split() for w in words_in_chunk)
        ]
        chunks.append(SubtitleChunk(index=i + 1, start=round(start, 3), end=round(end, 3), text=text,
                                    highlights=highlights, scene_number=scene.scene_number))
    return chunks


def _fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(chunks: list[SubtitleChunk]) -> str:
    return "\n".join(f"{c.index}\n{_fmt_srt(c.start)} --> {_fmt_srt(c.end)}\n{c.text}\n" for c in chunks)
