"""업로드 전 콘텐츠 검수: 사실/추측 구분, 출처, 독창성, 제목, 장면 일치, 라이선스, AI 고지.

YouTube 는 반복적·대량 생산 콘텐츠나 실질적 변형/독창성이 부족한 콘텐츠를 수익화에서 제외할 수 있다
(https://support.google.com/youtube/answer/1311392). 이 검사는 정책 위험을 줄이기 위한 보조 검사이며,
originality_score 는 수익화 승인 여부를 예측하는 값이 아니다.

심각도
- critical: 업로드 금지 → 작업을 review_needed 로 저장 (사람이 확인 후 `upload JOB --approve-review` 로만 진행)
- minor:    자동 수정 시도(예: 다른 제목 후보) 후 해결되지 않으면 기록만 하고 진행
- warning:  기록만
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher

from app.ai.metadata_generator import years
from app.ai.script_quality import HEDGES, is_complete_sentence, split_sentences
from app.ai.title_selector import CLICKBAIT, _EMOJI
from app.ai.validator import normalize_for_compare
from app.config.settings import Settings
from app.schemas import AssetInfo, AudioTrackInfo, ResearchNotes, ShortsContent, VideoMetadata
from app.topics.duplicate_checker import text_similarity

AI_SOURCES = {"ai_image", "cf_image", "hf_image"}
SELF_MADE = {"procedural"}


@dataclass
class ReviewIssue:
    code: str
    severity: str          # critical | minor | warning
    message: str
    fixable: bool = False


@dataclass
class ReviewReport:
    status: str = "passed"                 # passed | review_needed
    issues: list[ReviewIssue] = field(default_factory=list)
    originality_score: float = 1.0
    script_similarity: float = 0.0
    similar_to: str = ""
    title_similarity: float = 0.0
    fixes: list[str] = field(default_factory=list)
    approved_by_user: bool = False

    @property
    def critical(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity == "critical"]

    @property
    def fixable(self) -> list[ReviewIssue]:
        return [i for i in self.issues if i.severity == "minor" and i.fixable]

    def to_dict(self) -> dict:
        return {**asdict(self), "passed": self.status == "passed"}


def review_content(
    settings: Settings,
    content: ShortsContent,
    metadata: VideoMetadata,
    research: ResearchNotes | None,
    assets: list[AssetInfo],
    bgm: AudioTrackInfo | None,
    previous: list[dict] | None = None,
    hook_type: str = "",
    offline: bool = False,
) -> ReviewReport:
    cfg = settings.review
    r = ReviewReport()
    add = r.issues.append
    previous = previous or []

    # 1. 출처 누락
    if content.truth_status in cfg.require_sources_for and not (research and research.sources):
        add(ReviewIssue("sources_missing", "critical", f"{content.truth_status} 주제인데 자료 출처가 없음 (사실 확인 불가)"))

    # 2. 근거 없는 단정 / 사실 여부 고지
    if content.truth_status != "FACT":
        if not any(h in content.script for h in HEDGES):
            add(ReviewIssue("unfounded_assertion", "critical", f"{content.truth_status} 주제를 단정적으로 서술 (추측/전승 표현 없음)"))
        if "ℹ️" not in metadata.description:
            add(ReviewIssue("truth_notice_missing", "critical", "설명란에 사실 여부 고지 없음"))

    # 3. 과도한 낚시성 제목
    bad = [p for p in (*CLICKBAIT, *settings.script.banned_phrases) if p and p in metadata.title]
    if bad or len(_EMOJI.findall(metadata.title)) > cfg.max_title_emoji:
        add(ReviewIssue("clickbait_title", "minor", f"제목 과장/이모지: {', '.join(bad) or '이모지 과다'}", fixable=True))

    # 4. 대본과 실제 장면 불일치
    joined = normalize_for_compare("".join(s.narration for s in content.scenes))
    if content.scenes and SequenceMatcher(None, joined, normalize_for_compare(content.script)).ratio() < 0.9:
        add(ReviewIssue("scene_mismatch", "critical", "장면 나레이션이 대본과 다름"))
    asset_scenes = {a.scene_number for a in assets}
    missing = [s.scene_number for s in content.scenes if s.scene_number not in asset_scenes]
    if missing:
        add(ReviewIssue("scene_without_visual", "critical", f"시각 자료 없는 장면: {missing}"))
    if settings.assets.require_ai_image and not offline:
        non_ai = [a.scene_number for a in assets if not a.generated_by_ai]
        if non_ai:
            # 기존 AI 이미지 필수 정책을 우회하지 않는다
            add(ReviewIssue("ai_image_required", "critical", f"AI 이미지가 아닌 장면: {non_ai} (assets.require_ai_image)"))

    # 5. 기존 콘텐츠와 실질적으로 같은 구성
    for prev in previous:
        sim = text_similarity(content.script, prev.get("script", ""))
        if sim > r.script_similarity:
            r.script_similarity, r.similar_to = round(sim, 3), prev.get("title", "")
        r.title_similarity = max(r.title_similarity, round(text_similarity(metadata.title, prev.get("title", "")), 3))
    if r.script_similarity >= cfg.max_script_similarity:
        add(ReviewIssue("same_structure", "critical", f"최근 영상 '{r.similar_to}' 와 대본 유사도 {r.script_similarity:.2f}"))
    if r.title_similarity >= cfg.max_title_similarity:
        add(ReviewIssue("title_similar", "minor", f"최근 영상 제목과 유사 ({r.title_similarity:.2f})", fixable=True))

    # 6. 빈약한 결말
    sentences = split_sentences(content.script)
    if not sentences or not is_complete_sentence(sentences[-1]):
        add(ReviewIssue("weak_ending", "critical", "마지막 문장이 미완성"))
    elif len(normalize_for_compare(content.ending or sentences[-1])) < cfg.min_ending_chars:
        add(ReviewIssue("weak_ending", "warning", "결말 문장이 너무 짧음"))

    # 7. 반복적인 템플릿
    window = previous[: cfg.template_window]
    if hook_type and len(window) >= cfg.template_window:
        opening = normalize_for_compare(content.hook)[:6]
        if all(p.get("hook_type") == hook_type and normalize_for_compare(p.get("hook", ""))[:6] == opening for p in window):
            add(ReviewIssue("template_repetition", "warning", f"최근 {len(window)}개와 같은 도입 유형·첫 구절"))

    # 8. 저작권/사용 권한 정보
    for a in assets:
        if a.source not in AI_SOURCES | SELF_MADE and not a.license.strip():
            add(ReviewIssue("license_missing", "critical", f"장면 {a.scene_number} 자료({a.source}) 라이선스 정보 없음"))
    if bgm and not bgm.license.strip():
        add(ReviewIssue("license_missing", "critical", f"BGM '{bgm.title or bgm.file}' 라이선스 정보 없음"))
    if "AI" not in metadata.description:
        add(ReviewIssue("ai_disclosure_missing", "critical", "설명란에 AI 활용 고지 없음"))

    # 9. 한/영 제목이 같은 사건을 말하는가 (연도 비교, 설명은 생성 단계에서 이미 검증)
    if metadata.title_en and years(metadata.title) != years(metadata.title_en):
        add(ReviewIssue("bilingual_mismatch", "warning", "한국어/영어 제목의 연도가 다름"))

    title_unique = 1 - min(r.title_similarity / max(cfg.max_title_similarity, 0.01), 1)
    r.originality_score = round(0.7 * (1 - r.script_similarity) + 0.3 * title_unique, 3)
    r.status = "review_needed" if r.critical else "passed"
    return r


def fix_title(metadata: VideoMetadata, issues: list[ReviewIssue], previous_titles: list[str], settings: Settings) -> str | None:
    """제목 문제(minor)를 다른 후보로 바꿔 해결한다. 바꿨으면 새 제목을 돌려준다."""
    if not any(i.code in ("clickbait_title", "title_similar") for i in issues):
        return None
    for cand in sorted(metadata.title_selection, key=lambda c: c.get("score", 0), reverse=True):
        title = cand.get("title", "")
        if not title or title == metadata.title or cand.get("excluded"):
            continue
        if any(p in title for p in (*CLICKBAIT, *settings.script.banned_phrases) if p):
            continue
        if max((text_similarity(title, p) for p in previous_titles if p), default=0) >= settings.review.max_title_similarity:
            continue
        return title
    return None
