"""정보 카드: 확인된 사실/미확인 주장, 가설 비교, 사건 타임라인, 핵심 단서를 화면에 보여준다.

장식이 아니라 시청자 이해를 돕는 정보만 넣는다:
- 카드 항목은 대본(나레이션)에 실제로 나온 것만, '확인된 사실'은 자료 조사의 FACT 항목과 겹치는 것만 넣는다.
- 첫 장면(Hook)과 마지막 장면에는 넣지 않고, 영상당 video.info_cards.max_cards 개까지.
- 렌더링은 기존 자막 트랙(Pillow PNG)에 함께 그려 추가 FFmpeg 단계·유료 API 가 없다.
지도·인물 관계도는 정확한 위치/관계 데이터가 없어 만들지 않는다 (잘못된 정보를 그리지 않기 위해).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from app.ai.metadata_generator import years
from app.config.settings import InfoCardConfig
from app.schemas import Explainer, ResearchNotes, Scene
from app.topics.duplicate_checker import extract_keywords, keyword_overlap


@dataclass
class InfoCard:
    scene_number: int
    kind: str                                   # facts | hypotheses | timeline | clue
    title: str
    rows: list[tuple[str, str]] = field(default_factory=list)   # (라벨, 내용)

    def to_dict(self) -> dict:
        return asdict(self)


def _in_script(text: str, script: str, threshold: float = 0.5) -> bool:
    words = extract_keywords(text)
    return bool(words) and keyword_overlap(words, extract_keywords(script)) >= threshold


def _grounded_fact(text: str, research: ResearchNotes | None) -> bool:
    if not research:
        return False
    words = extract_keywords(text)
    facts = [f.statement for f in research.facts if f.status == "FACT"]
    return bool(words) and any(keyword_overlap(words, extract_keywords(f)) >= 0.5 for f in facts)


def _short(text: str, limit: int = 26) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_cards(explainer: Explainer, research: ResearchNotes | None, script: str, story_format: str,
                max_items: int = 4) -> list[InfoCard]:
    """카드 후보 (장면 배정 전). 검증되지 않은 항목은 빼고, 항목이 부족한 카드는 만들지 않는다."""
    cards: list[InfoCard] = []
    facts = [f for f in explainer.confirmed_facts if _in_script(f, script) and _grounded_fact(f, research)]
    unconfirmed = [u for u in explainer.unconfirmed_claims if _in_script(u, script)]
    if facts and (unconfirmed or len(facts) >= 2):
        rows = [("확인", _short(f)) for f in facts[: max_items - min(len(unconfirmed), 2)]]
        rows += [("미확인", _short(u)) for u in unconfirmed[:2]]
        cards.append(InfoCard(0, "facts", "기록으로 확인된 것 vs 확인 안 된 것", rows[:max_items]))
    hyps = [h for h in explainer.hypotheses if h.name.strip() and _in_script(h.name, script, 0.5)]
    if len(hyps) >= 2:
        rows = []
        for i, h in enumerate(hyps[:2]):
            label = f"가설 {'AB'[i]}"
            rows.append((label, _short(h.name)))
            if h.evidence and _in_script(h.evidence, script, 0.4):
                rows.append(("근거", _short(h.evidence)))
            if h.weakness and _in_script(h.weakness, script, 0.4):
                rows.append(("약점", _short(h.weakness)))
        cards.append(InfoCard(0, "hypotheses", "두 가지 설명", rows[:max_items + 2]))
    script_years = years(script)
    events = [e for e in explainer.timeline if e.when and e.what and years(e.when) and years(e.when) <= script_years]
    if len(events) >= 3:
        cards.append(InfoCard(0, "timeline", "사건 타임라인", [(_short(e.when, 12), _short(e.what)) for e in events[:max_items]]))
    clues = [c for c in explainer.key_clues if _in_script(c, script)]
    if clues:
        cards.append(InfoCard(0, "clue", "핵심 단서", [("단서", _short(c)) for c in clues[:3]]))
    # 전개 형식에 맞는 카드를 앞에 둔다
    preferred = {"timeline": "timeline", "two_hypotheses": "hypotheses", "evidence": "clue",
                 "science_vs_legend": "facts", "record": "facts", "classic": "facts"}.get(story_format, "facts")
    cards.sort(key=lambda c: (c.kind != preferred, ["facts", "hypotheses", "timeline", "clue"].index(c.kind)))
    return cards


def _best_scene(card: InfoCard, middle: list[Scene], used: set[int]) -> Scene | None:
    free = [s for s in middle if s.scene_number not in used]
    if not free:
        return None
    if card.kind == "timeline":
        return max(free, key=lambda s: len(years(s.narration)))
    text = " ".join(r[1] for r in card.rows)
    scored = max(free, key=lambda s: keyword_overlap(extract_keywords(text), extract_keywords(s.narration)))
    if keyword_overlap(extract_keywords(text), extract_keywords(scored.narration)) > 0:
        return scored
    return free[0] if card.kind == "facts" else free[-1]


def plan_info_cards(scenes: list[Scene], explainer: Explainer | None, research: ResearchNotes | None,
                    story_format: str, cfg: InfoCardConfig) -> list[InfoCard]:
    if not cfg.enabled or not explainer or len(scenes) < 3:
        return []
    script = " ".join(s.narration for s in scenes)
    middle = scenes[1:-1]
    planned: list[InfoCard] = []
    used: set[int] = set()
    for card in build_cards(explainer, research, script, story_format, cfg.max_items):
        if len(planned) >= cfg.max_cards:
            break
        scene = _best_scene(card, middle, used)
        if scene is None:
            break
        card.scene_number = scene.scene_number
        used.add(scene.scene_number)
        planned.append(card)
    return sorted(planned, key=lambda c: c.scene_number)
