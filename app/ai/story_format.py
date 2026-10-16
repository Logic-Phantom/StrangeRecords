"""영상마다 주제에 맞는 전개 형식을 고른다 (모든 영상이 같은 Hook → 배경 → 반전 → 루프를 복제하지 않도록).

형식은 이름만 바꾸는 것이 아니라 대본이 담아야 할 정보(explainer 항목)가 다르다.
- 최근 영상에서 쓴 형식은 쉬게 하고, 카테고리/사실 여부에 맞는 형식 중에서 작업 ID 로 결정적으로 고른다.
- 기존 구조(classic)는 언제든 선택 가능한 형식으로 남긴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class StoryFormat:
    id: str
    name: str
    guide: str                   # 대본 프롬프트에 넣는 전개 지시
    required: tuple[str, ...]    # explainer 에 반드시 채워야 하는 항목
    categories: tuple[str, ...]  # 어울리는 카테고리 (빈 값이면 모두)
    truth: tuple[str, ...] = ()  # 어울리는 사실 여부 (빈 값이면 모두)


FORMATS: dict[str, StoryFormat] = {f.id: f for f in (
    StoryFormat(
        "classic", "의문-배경-반전-회수",
        "첫 문장에 핵심 의문 → 최소 배경 → 새 단서/반전 → 처음 질문 회수.",
        ("confirmed_facts", "key_clues"), (),
    ),
    StoryFormat(
        "timeline", "사건 타임라인 재구성",
        "확인된 시점 3~4개를 시간 순서로 따라가며 '언제 무엇이 달라졌는지'를 보여준다. 시점마다 새 정보를 주고,"
        " 기록이 끊기거나 엇갈리는 지점을 결말에서 짚는다. 날짜·시각은 자료에 있는 것만 쓴다.",
        ("timeline", "confirmed_facts"), ("unsolved_mystery", "hidden_history"), ("FACT", "UNCONFIRMED"),
    ),
    StoryFormat(
        "evidence", "증거 비교",
        "핵심 증거 2~3개를 하나씩 제시하고, 각 증거가 무엇을 말해 주고 무엇을 말해 주지 못하는지 구분한다."
        " 증거끼리 맞지 않는 부분이 결말의 질문이 된다.",
        ("key_clues", "confirmed_facts"), ("unsolved_mystery", "strange_phenomena", "hidden_history"),
    ),
    StoryFormat(
        "two_hypotheses", "두 가설 검토",
        "영상에서 소개할 대표 가설 두 개를 정하고, 각 가설의 근거와 약점을 같은 비중으로 설명한다."
        " 어느 쪽도 확정하지 않으며, 무엇이 확인되면 결론이 날 수 있는지로 마무리한다.",
        ("hypotheses", "unresolved"), ("unsolved_mystery", "strange_phenomena", "legendary_creatures"),
    ),
    StoryFormat(
        "record", "역사 기록 해석",
        "남아 있는 기록(문서·일지·비문 등)이 실제로 무엇이라고 적었는지와, 후대에 덧붙은 해석·과장을 구분해 보여준다.",
        ("confirmed_facts", "unconfirmed_claims"), ("hidden_history", "legendary_creatures"),
    ),
    StoryFormat(
        "science_vs_legend", "과학적 설명과 전설 비교",
        "전해지는 이야기(전설·괴담)를 먼저 소개하고, 과학적으로 확인된 설명과 나란히 비교한다."
        " 이야기를 사실처럼 단정하지 않고, 과학으로도 설명되지 않는 부분이 있으면 솔직히 남긴다.",
        ("unconfirmed_claims", "confirmed_facts"), ("strange_phenomena", "legendary_creatures"),
        ("LEGEND", "URBAN_LEGEND", "UNCONFIRMED"),
    ),
)}

# brain_teaser(심리테스트/퀴즈)는 기존 구조만 사용한다
_DEFAULT_ONLY = ("brain_teaser",)


def eligible(category: str, truth_status: str) -> list[str]:
    if category in _DEFAULT_ONLY:
        return ["classic"]
    out = []
    for f in FORMATS.values():
        if f.categories and category not in f.categories:
            continue
        if f.truth and truth_status not in f.truth:
            continue
        out.append(f.id)
    return out or ["classic"]


def choose_format(category: str, truth_status: str, recent_formats: list[str] | None = None, seed: str = "",
                  rest_window: int = 2) -> str:
    """카테고리·사실 여부에 맞는 형식 중 최근 rest_window 개 영상에서 쓰지 않은 것을 작업 ID 로 결정적으로 고른다."""
    options = eligible(category, truth_status)
    recent = [r for r in (recent_formats or []) if r][:rest_window]
    fresh = [o for o in options if o not in recent] or options
    digest = int(hashlib.sha1((seed or category).encode("utf-8")).hexdigest(), 16)
    return fresh[digest % len(fresh)]


def format_instruction(format_id: str) -> str:
    f = FORMATS.get(format_id, FORMATS["classic"])
    fields = ", ".join(f.required)
    return f"{f.name} ({f.id}): {f.guide}\n   - explainer 에서 반드시 채울 항목: {fields}"


def missing_required(format_id: str, explainer) -> list[str]:
    """형식이 요구하는 explainer 항목 중 비어 있는 것 (two_hypotheses 는 가설 2개 이상)."""
    f = FORMATS.get(format_id, FORMATS["classic"])
    missing = [name for name in f.required if not getattr(explainer, name, None)]
    if format_id == "two_hypotheses" and len(explainer.hypotheses) < 2 and "hypotheses" not in missing:
        missing.append("hypotheses(2개 이상)")
    if format_id == "timeline" and 0 < len(explainer.timeline) < 3:
        missing.append("timeline(3개 이상)")
    return missing
