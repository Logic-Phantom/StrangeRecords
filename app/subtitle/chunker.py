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
# 뒤 단어와 붙어 다니는 수사/관형사: 이 뒤에서 자막을 끊으면 "한 / 명도" 처럼 어색하다
_NO_BREAK_AFTER = {
    "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉", "열", "스무", "몇", "수",
    "이", "그", "저", "단", "약", "첫", "모든", "각", "매", "온", "그런", "이런", "어느", "무슨", "새", "옛",
}
_BREAK_PENALTY = 1000.0


def _clen(text: str) -> int:
    return len(_STRIP.sub("", text))


def _balanced(words: list[str], max_chars: int, max_words: int) -> list[list[str]]:
    """한 구절(문장/쉼표 단위)을 가장 적은 덩어리 수로, 덩어리 길이가 최대한 고르게 나눈다.

    앞에서부터 꽉 채우는 방식은 끝에 한 어절만 남아 자막이 툭 끊기듯 바뀌므로,
    가능한 분할 중 글자 수 편차가 가장 작은 것을 고른다 (어절 수가 적어 DP 로 충분).
    """
    lens = [len(w) for w in words]
    n_words = len(words)

    def fits(i: int, j: int) -> bool:  # words[i:j] 가 한 덩어리가 될 수 있는가
        return j - i == 1 or (sum(lens[i:j]) <= max_chars and j - i <= max_words)

    fallback: list[list[str]] | None = None
    for n in range(max(1, -(-sum(lens) // max(max_chars, 1))), n_words + 1):
        target = sum(lens) / n
        # best[k][j] = words[:j] 를 k 덩어리로 나눌 때 (편차 제곱합, 분할 위치)
        best: list[dict[int, tuple[float, list[int]]]] = [{0: (0.0, [])}] + [{} for _ in range(n)]
        for k in range(1, n + 1):
            for j in range(1, n_words + 1):
                for i, (cost, cuts) in best[k - 1].items():
                    if i < j and fits(i, j):
                        c = cost + (sum(lens[i:j]) - target) ** 2
                        if j < n_words and words[j - 1] in _NO_BREAK_AFTER:
                            c += _BREAK_PENALTY
                        if j not in best[k] or c < best[k][j][0]:
                            best[k][j] = (c, cuts + [j])
        if n_words in best[n]:
            cost, cuts = best[n][n_words]
            parts = [words[a:b] for a, b in zip([0] + cuts, cuts)]
            if cost < _BREAK_PENALTY or fallback is not None:
                # 수사 뒤에서 끊어야만 하면 한 덩어리 더 나눠 본다 (그래도 안 되면 처음 결과)
                return parts if cost < _BREAK_PENALTY else fallback
            fallback = parts
    return fallback or [[w] for w in words]


def split_chunks(text: str, max_chars: int = 11, max_words: int = 3) -> list[str]:
    """어절 단위로 묶는다. 문장 끝/쉼표에서는 반드시 끊고, 구절 안에서는 길이를 고르게 나눈다."""
    clauses: list[list[str]] = [[]]
    for word in text.split():
        clauses[-1].append(word)
        if _SENTENCE_END.search(word) or word.endswith(","):
            clauses.append([])
    chunks = [" ".join(part) for clause in clauses if clause for part in _balanced(clause, max_chars, max_words)]
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
    english: bool = True,
    max_gap_in_scene: float = 1.5,
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
        same_scene = i + 1 < len(raw) and raw[i + 1][3].scene_number == scene.scene_number
        # 다음 자막 직전까지 유지해서 깜빡임을 줄인다: 같은 Scene 안에서는 빈 화면 없이 바로 이어지고,
        # Scene 사이(장면 전환)는 짧은 쉼(0.6초 미만)이면 이어 붙인다
        end = max(end, start + min_display)
        if next_start > start:
            end = min(end, next_start)
        if 0 <= next_start - end < (max_gap_in_scene if same_scene else 0.6):
            end = next_start
        words_in_chunk = text.split()
        highlights = [
            h for h in scene.emphasis_words
            if h and any(t == w.strip(".,!?") or (len(t) >= 2 and t in w) for t in h.split() for w in words_in_chunk)
        ]
        chunks.append(SubtitleChunk(index=i + 1, start=round(start, 3), end=round(end, 3), text=text,
                                    highlights=highlights, scene_number=scene.scene_number,
                                    text_en=scene.narration_en.strip() if english else ""))
    return chunks


def _fmt_srt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(chunks: list[SubtitleChunk]) -> str:
    """한국어 자막 + (있으면) 영어 자막 두 줄."""
    return "\n".join(
        f"{c.index}\n{_fmt_srt(c.start)} --> {_fmt_srt(c.end)}\n{c.text}\n" + (f"{c.text_en}\n" if c.text_en else "")
        for c in chunks
    )
