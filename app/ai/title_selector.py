"""제목 후보 평가/선택 (실제 CTR·검색량을 알 수 없으므로 추정치를 쓰지 않고, 내용 일치·정확성 규칙만으로 평가).

평가 항목 (0~1, 가중 평균)
- content   제목의 핵심어가 실제 나레이션에 있는가 (내용과 일치)
- hook      첫 문장과 같은 대상을 말하는가
- length    40자 이내
- keyword   주제의 핵심 검색어(고유명사/대상)를 포함하는가
- honest    과장/낚시 표현이 없는가 (있으면 후보에서 제외)
- novelty   최근 영상 제목과 지나치게 비슷하지 않은가
- bilingual 영어 제목이 있고 숫자(연도 등)가 한국어 제목과 일치하는가
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas import ShortsContent, TitleCandidate, VideoMetadata
from app.topics.duplicate_checker import extract_keywords, keyword_overlap, text_similarity

WEIGHTS = {"content": 0.25, "hook": 0.15, "length": 0.10, "keyword": 0.15, "honest": 0.15, "novelty": 0.15, "bilingual": 0.05}
CLICKBAIT = ("충격", "경악", "100%", "소름 돋는 진실", "절대 클릭", "실화냐", "미쳤다", "역대급", "!!", "?!", "‼")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿]")
_NUM = re.compile(r"\d{2,}")


def numbers(text: str) -> set[str]:
    return set(_NUM.findall(text or ""))


@dataclass
class TitleScore:
    candidate: TitleCandidate
    parts: dict[str, float] = field(default_factory=dict)
    total: float = 0.0
    excluded: str = ""

    def to_dict(self) -> dict:
        return {"strategy": self.candidate.strategy, "title": self.candidate.title, "title_en": self.candidate.title_en,
                "score": round(self.total, 3), "parts": {k: round(v, 2) for k, v in self.parts.items()},
                "excluded": self.excluded}


def score_title(c: TitleCandidate, content: ShortsContent, previous_titles: list[str], banned: list[str],
                max_emoji: int = 1) -> TitleScore:
    s = TitleScore(c)
    tkw = extract_keywords(c.title)
    script_kw = extract_keywords(content.script)
    s.parts["content"] = keyword_overlap(tkw, script_kw) if tkw else 0.0
    s.parts["hook"] = min(keyword_overlap(tkw, extract_keywords(content.hook)) / 0.5, 1.0) if tkw else 0.0
    n = len(c.title)
    s.parts["length"] = 1.0 if n <= 40 else max(0.0, 1 - (n - 40) / 20)
    topic_kw = extract_keywords(content.topic)
    s.parts["keyword"] = 1.0 if keyword_overlap(tkw, topic_kw) > 0 else 0.3
    bad = [p for p in (*CLICKBAIT, *banned) if p and p in c.title]
    emoji = len(_EMOJI.findall(c.title))
    s.parts["honest"] = 0.0 if bad or emoji > max_emoji else 1.0
    if bad:
        s.excluded = f"과장 표현: {', '.join(bad)}"
    elif emoji > max_emoji:
        s.excluded = f"이모지 {emoji}개"
    sim = max((text_similarity(c.title, p) for p in previous_titles if p), default=0.0)
    s.parts["novelty"] = 0.0 if sim >= 0.75 else 1 - sim
    if sim >= 0.75 and not s.excluded:
        s.excluded = f"최근 제목과 유사 ({sim:.2f})"
    en_ok = bool(c.title_en.strip()) and not re.search(r"[가-힣]", c.title_en)
    s.parts["bilingual"] = (1.0 if numbers(c.title) == numbers(c.title_en) else 0.5) if en_ok else 0.0
    s.total = sum(WEIGHTS[k] * v for k, v in s.parts.items())
    return s


def select_title(meta: VideoMetadata, content: ShortsContent, previous_titles: list[str] | None = None,
                 banned: list[str] | None = None, max_emoji: int = 1) -> VideoMetadata:
    """후보 중 가장 적절한 제목을 meta.title/title_en 에 반영하고 점수와 선택 근거를 기록한다."""
    candidates = [c for c in meta.title_candidates if c.title.strip()]
    if meta.title.strip() and not any(c.title.strip() == meta.title.strip() for c in candidates):
        candidates.append(TitleCandidate(strategy="default", title=meta.title, title_en=meta.title_en))
    if not candidates:
        return meta
    scores = [score_title(c, content, previous_titles or [], banned or [], max_emoji) for c in candidates]
    eligible = [s for s in scores if not s.excluded] or scores
    best = max(eligible, key=lambda s: s.total)
    meta.title = best.candidate.title.strip()
    if best.candidate.title_en.strip():
        meta.title_en = best.candidate.title_en.strip()
    meta.title_strategy = best.candidate.strategy or "default"
    meta.title_selection = [s.to_dict() | {"selected": s is best} for s in scores]
    return meta
