"""광고 적합성 보조 검사 (YouTube 광고 친화적 콘텐츠 가이드라인 관점의 위험 신호).

참고: https://support.google.com/youtube/answer/6162278 (광고 친화적 콘텐츠 가이드라인),
      https://support.google.com/youtube/answer/1311392 (YouTube 파트너 프로그램 수익 창출 정책)
이 검사는 단어·표현 규칙 기반의 보조 검사다. 통과해도 수익화 승인이나 광고 게재/수익을 보장하지 않는다.

상태: pass | warning | review_needed | block
- block: 시각 프롬프트에 노골적인 유혈/시신/훼손 묘사 → 업로드 보류 (critical)
- review_needed: 근거 없는 범죄 혐의 단정, 사실이 아닌 주제를 '실화'로 소개, 실제 참사의 잔혹 묘사 → 업로드 보류 (critical)
- warning: 충격 위주 제목 표현, 민감한 실제 사건 (기록만)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.script_quality import HEDGES, split_sentences

# 시각 프롬프트(영어)의 노골적 묘사 → AI 이미지가 실제로 그 장면을 그린다
GRAPHIC_VISUAL = re.compile(
    r"\b(blood(y|ied)?|gore|gory|corpses?|dead bod(y|ies)|severed|decapitat\w*|mutilat\w*|dismember\w*|tortur\w*|"
    r"entrails|guts|open wound|bleeding|hanged body|suicide|gunshot wound)\b",
    re.IGNORECASE,
)
# 나레이션의 잔혹 묘사
GRAPHIC_NARRATION = ("토막", "참수", "시신 훼손", "시신을 훼손", "피투성이", "피범벅", "내장", "난도질", "고문", "잔혹하게", "절단된")
# 충격만을 노린 제목 표현 (기존 CLICKBAIT 의 충격/경악 외)
SHOCK_TITLE = ("끔찍", "참혹", "엽기", "잔혹", "소름", "피의", "공포의 실체")
# 실제 피해자가 있는 사건
TRAGEDY = ("참사", "학살", "테러", "자살", "살해", "살인", "피해자", "유가족", "희생자")
# 범죄 혐의 단정 ("범인은 OO였습니다", "OO가 죽였습니다") - 범인이 밝혀지지 않았다는 문장은 제외
ACCUSATION = re.compile(r"(범인|진범|살인범)(은|는)\s?[^.?!]*?(였습니다|이었습니다|였다|이었다|입니다|이다)"
                        r"|\S+(이|가)\s?(죽였|살해했)습니다")
NOT_ACCUSING = ("미상", "알려지지", "밝혀지지", "잡히지", "모릅", "불명", "없습니다", "없었", "확인되지")


@dataclass
class AdIssue:
    code: str
    severity: str        # critical | warning
    message: str


@dataclass
class AdReport:
    status: str = "pass"
    issues: list[AdIssue] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "reasons": [f"{i.code}: {i.message}" for i in self.issues],
            "note": "규칙 기반 보조 지표 - 수익화 승인·광고 수익을 보장하지 않음",
        }


def check_ad_suitability(title: str, script: str, description: str, truth_status: str,
                         visual_prompts: list[str], strict: bool = False) -> AdReport:
    report = AdReport()
    add = report.issues.append
    hedged = lambda s: any(h in s for h in HEDGES)  # noqa: E731

    # 1. 노골적인 시각 묘사 (AI 이미지로 그려짐)
    graphic = sorted({m.group(0).lower() for p in visual_prompts for m in GRAPHIC_VISUAL.finditer(p or "")})
    if graphic:
        add(AdIssue("ad_graphic_visual", "critical", f"장면 이미지 프롬프트에 잔혹/유혈 묘사: {', '.join(graphic)}"))

    # 2. 나레이션의 잔혹 묘사 (실제 사건이면 더 엄격)
    gore = [w for w in GRAPHIC_NARRATION if w in script]
    real = truth_status in ("FACT", "UNCONFIRMED")
    tragedy = [w for w in TRAGEDY if w in script or w in title]
    if gore:
        severity = "critical" if (real and tragedy) or strict else "warning"
        add(AdIssue("ad_graphic_narration", severity, f"잔혹한 묘사 표현: {', '.join(gore)}"
                    + (" (실제 피해자가 있는 사건)" if real and tragedy else "")))
    elif real and tragedy:
        add(AdIssue("ad_sensitive_topic", "warning", f"실제 피해가 있는 사건 ({', '.join(tragedy)}) - 교육·기록 맥락과 피해자 존중 확인"))

    # 3. 충격만을 노린 제목
    shock = [w for w in SHOCK_TITLE if w in title]
    if shock:
        add(AdIssue("ad_shock_title", "warning", f"충격 위주 제목 표현: {', '.join(shock)}"))

    # 4. 근거 없는 범죄 혐의 단정
    for sentence in split_sentences(script):
        if (ACCUSATION.search(sentence) and not hedged(sentence) and not any(w in sentence for w in NOT_ACCUSING)
                and not sentence.rstrip().endswith("?")):
            severity = "critical" if truth_status != "FACT" or strict else "warning"
            add(AdIssue("unfounded_accusation", severity, f"범죄 혐의를 단정하는 문장: '{sentence[:40]}'"))
            break

    # 5. 사실이 아닌 주제를 '실화'로 소개 (시청자 오해)
    if truth_status != "FACT" and ("실화" in title or "실화" in description.split("\n\n")[0]):
        add(AdIssue("misleading_true_story", "critical", f"{truth_status} 주제를 '실화'로 소개"))

    if any(i.code == "ad_graphic_visual" for i in report.issues):
        report.status = "block"
    elif any(i.severity == "critical" for i in report.issues):
        report.status = "review_needed"
    elif report.issues:
        report.status = "warning"
    return report
