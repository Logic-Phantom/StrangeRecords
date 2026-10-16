"""업로드 전 콘텐츠 검수: 사실/추측 구분, 출처, 독창성, 제목, 장면 일치, 라이선스, AI 고지,
사실-출처 연결, 출처 없는 구체 주장, 미확인 정보 단정, 설명란 출처 링크, AI 이미지-장면 일치, 음성 권리, 광고 적합성.

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
from urllib.parse import urlparse

from app.ai.metadata_generator import years
from app.ai.script_quality import HEDGES, is_complete_sentence, split_sentences
from app.ai.title_selector import CLICKBAIT, _EMOJI
from app.ai.validator import normalize_for_compare
from app.config.settings import Settings
from app.quality.ad_suitability import check_ad_suitability
from app.schemas import AssetInfo, AudioTrackInfo, ResearchNotes, ShortsContent, VideoMetadata
from app.topics.duplicate_checker import extract_keywords, keyword_overlap, text_similarity
from app.voice.rights import voice_rights

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
    ad_suitability: dict = field(default_factory=dict)
    # 사람이 승인해도 해결된 것으로 보지 않는 critical 항목 (승인 = 업로드 진행 기록일 뿐)
    approved_unresolved: list[str] = field(default_factory=list)

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
    draft: dict | None = None,
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

    # 10~16. 사실성·출처·장면 일치·권리·고유 설명·광고 적합성 (2026-10-16 보완)
    _check_facts_and_sources(settings, content, metadata, research, add)
    _check_visual_alignment(settings, content, assets, add, offline)
    _check_rights(settings, assets, bgm, add, offline)
    _check_explainer(settings, draft, previous, add)
    if cfg.ad_suitability:
        ad = check_ad_suitability(metadata.title, content.script, metadata.description, content.truth_status,
                                  [s.visual_prompt for s in content.scenes], strict=cfg.strict)
        r.ad_suitability = ad.to_dict()
        for i in ad.issues:
            add(ReviewIssue(i.code, i.severity, i.message))

    title_unique = 1 - min(r.title_similarity / max(cfg.max_title_similarity, 0.01), 1)
    r.originality_score = round(0.7 * (1 - r.script_similarity) + 0.3 * title_unique, 3)
    r.status = "review_needed" if r.critical else "passed"
    return r


def _sev(settings: Settings) -> str:
    """엄격 모드에서는 critical(업로드 보류), 아니면 warning(기록만)."""
    return "critical" if settings.review.strict else "warning"


def _check_facts_and_sources(settings: Settings, content: ShortsContent, metadata: VideoMetadata,
                             research: ResearchNotes | None, add) -> None:
    if not research:
        return
    sources = research.sources
    # 사실 주장 ↔ 출처 연결: FACT 로 정리된 사실이 실제 수집 자료(source_index)에 연결되어 있는가
    unlinked = [f for f in research.facts if f.status == "FACT"
                and (f.source_index is None or not (0 <= f.source_index < len(sources)))]
    if unlinked:
        add(ReviewIssue("fact_unlinked", _sev(settings),
                        f"출처가 연결되지 않은 FACT {len(unlinked)}건: '{unlinked[0].statement[:30]}'"))

    # 출처 없는 구체 주장: 대본의 연도가 수집 자료(본문·사실 목록·요약)에 없음
    corpus = " ".join([s.extract for s in sources] + [f.statement for f in research.facts] + [research.summary])
    if settings.review.check_unsourced_years and (any(s.extract for s in sources) or research.facts):
        missing = sorted(years(content.script) - years(corpus))
        if missing:
            add(ReviewIssue("unsourced_claim", _sev(settings), f"수집 자료에서 확인되지 않는 연도: {', '.join(missing)}"))
        desc_body = metadata.description.split("\n\n")[0]
        extra = sorted(years(desc_body) - years(corpus) - years(content.script))
        if extra:
            add(ReviewIssue("description_unsupported", "warning", f"설명란에만 있는 연도(대본·자료에 없음): {', '.join(extra)}"))

    # 미확인 정보를 사실처럼 단정: 미확인/전설 항목과 같은 내용을 말하는 대본 문장에 추측·전승 표현이 없음
    sentences = split_sentences(content.script)
    for fact in research.facts:
        if fact.status == "FACT":
            continue
        words = extract_keywords(fact.statement)
        if len(words) < 2:
            continue
        best = max(sentences, key=lambda s: keyword_overlap(words, extract_keywords(s)), default="")
        if best and keyword_overlap(words, extract_keywords(best)) >= 0.6 and not any(h in best for h in HEDGES):
            add(ReviewIssue("unconfirmed_as_fact", _sev(settings),
                            f"{fact.status} 내용을 단정적으로 서술: '{best[:40]}'"))
            break

    # 설명란 출처 링크 정확성: 수집한 자료 URL 이 설명란에 그대로 있고 형식이 올바른가
    for src in sources:
        if not src.url:
            continue
        host = urlparse(src.url).netloc
        if not src.url.startswith("https://") or (src.provider == "wikipedia" and not host.endswith("wikipedia.org")):
            add(ReviewIssue("source_link_invalid", "warning", f"출처 링크 형식 이상: {src.url[:80]}"))
        elif src.url not in metadata.description:
            add(ReviewIssue("source_link_missing", _sev(settings), f"설명란에 출처 링크 없음: {src.title}"))


def _check_visual_alignment(settings: Settings, content: ShortsContent, assets: list[AssetInfo], add, offline: bool) -> None:
    scenes = {s.scene_number: s for s in content.scenes}
    for a in assets:
        scene = scenes.get(a.scene_number)
        if not scene or not a.generated_by_ai:
            continue
        prompt = (scene.visual_prompt or scene.visual).strip()
        # AI 이미지가 이 장면의 프롬프트로 그려졌는가 (다른 장면 이미지가 섞이지 않았는가)
        if prompt and a.query and not a.query.startswith(prompt[:40]):
            add(ReviewIssue("visual_scene_mismatch", "critical", f"장면 {a.scene_number} 이미지가 다른 프롬프트로 생성됨"))
        # 영어 나레이션과 이미지 프롬프트가 전혀 다른 내용을 말하는가 (둘 다 영어라 비교 가능)
        if scene.visual_prompt and scene.narration_en:
            shared = extract_keywords(scene.narration_en) & extract_keywords(scene.visual_prompt)
            if not shared:
                add(ReviewIssue("visual_prompt_unrelated", "warning", f"장면 {a.scene_number} 이미지 설명이 나레이션과 겹치는 단어 없음"))
    ai_used = any(a.generated_by_ai for a in assets)
    if ai_used and not offline and not settings.video.ai_label.enabled and content.truth_status == "FACT" \
            and "photo" in (settings.assets.ai_image.style or "").lower():
        add(ReviewIssue("ai_image_realism", "warning",
                        "실제 사건에 사실적 AI 이미지를 쓰는데 화면 AI 표시(video.ai_label)가 꺼져 있음 → 실제 기록으로 오해 가능"))


_UNVERIFIED_LICENSE = ("unknown", "unverified", "확인 필요", "미확인", "불명", "todo")


def _check_rights(settings: Settings, assets: list[AssetInfo], bgm: AudioTrackInfo | None, add, offline: bool) -> None:
    for a in assets:
        if any(w in a.license.lower() for w in _UNVERIFIED_LICENSE):
            add(ReviewIssue("license_unverified", "critical", f"장면 {a.scene_number} 자료 라이선스 미확인: {a.license}"))
    if bgm and any(w in bgm.license.lower() for w in _UNVERIFIED_LICENSE):
        add(ReviewIssue("license_unverified", "critical", f"BGM 라이선스 미확인: {bgm.license}"))
    if offline:
        return
    rights = voice_rights(settings)
    if rights.review_severity in ("warning", "critical"):
        add(ReviewIssue("voice_rights_unverified" if rights.status != "restricted" else "voice_rights_restricted",
                        rights.review_severity,
                        f"Edge TTS 음성의 상업적 이용 권리: {rights.status} (python -m app.main rights)"))


def _check_explainer(settings: Settings, draft: dict | None, previous: list[dict], add) -> None:
    if not draft:
        return
    from app.ai.story_format import missing_required
    from app.schemas import Explainer

    fmt = draft.get("story_format") or "classic"
    explainer = Explainer(**(draft.get("explainer") or {}))
    if explainer.is_empty():
        add(ReviewIssue("explainer_missing", "warning", "영상별 고유한 설명(확인된 사실·단서·가설) 정리가 없음"))
        return
    missing = missing_required(fmt, explainer)
    if missing:
        add(ReviewIssue("explainer_incomplete", "warning", f"전개 형식 {fmt} 에 필요한 항목 부족: {', '.join(missing)}"))
    if not explainer.unique_insight.strip():
        add(ReviewIssue("unique_insight_missing", "warning", "이 영상만의 고유한 설명/결론(unique_insight)이 없음"))
    recent = [p.get("story_format") for p in previous[:3] if p.get("story_format")]
    if len(recent) >= 3 and all(r == fmt for r in recent):
        add(ReviewIssue("format_repetition", "warning", f"최근 {len(recent)}개 영상과 같은 전개 형식({fmt})"))


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
