"""파이프라인 전체에서 주고받는 콘텐츠 데이터 구조 (Gemini JSON 응답 검증 포함)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

TruthStatus = Literal["FACT", "UNCONFIRMED", "LEGEND", "URBAN_LEGEND", "FICTION"]
TRUTH_STATUSES: tuple[str, ...] = ("FACT", "UNCONFIRMED", "LEGEND", "URBAN_LEGEND", "FICTION")

SFX_NAMES: tuple[str, ...] = ("heartbeat", "impact", "whoosh", "suspense", "click", "transition")
EFFECTS: tuple[str, ...] = ("auto", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static")
HOOK_TYPES: tuple[str, ...] = ("question", "anomaly", "contradiction", "number", "direct_address", "other")


def _normalize_truth(value: object) -> object:
    if isinstance(value, str):
        cleaned = value.strip().upper().replace(" ", "_").replace("-", "_")
        if cleaned in TRUTH_STATUSES:
            return cleaned
        if cleaned in ("URBANLEGEND",):
            return "URBAN_LEGEND"
    return value


# ----------------------------------------------------------------- topic
class TopicCandidate(BaseModel):
    title: str
    topic: str
    category: str
    keywords: list[str] = Field(default_factory=list)
    summary: str = ""
    truth_status: TruthStatus = "UNCONFIRMED"
    search_terms_ko: list[str] = Field(default_factory=list)
    search_terms_en: list[str] = Field(default_factory=list)
    hook_idea: str = ""
    # 40~60초 전개 (의문 → 배경 → 단서/반전 → 회수). 주제 점수의 이야기 구조 항목에 사용
    story_beats: list[str] = Field(default_factory=list)
    # 트렌드 힌트에서 착안했다면 그 힌트 키워드 (선정 근거 기록용)
    trend_ref: str = ""

    normalize_truth = field_validator("truth_status", mode="before")(_normalize_truth)


class TopicCandidates(BaseModel):
    candidates: list[TopicCandidate]


class DuplicateJudgement(BaseModel):
    index: int
    duplicate: bool
    similar_to: str = ""
    reason: str = ""


class DuplicateJudgements(BaseModel):
    results: list[DuplicateJudgement]


# -------------------------------------------------------------- research
class SourceDoc(BaseModel):
    title: str
    url: str
    lang: str = ""
    provider: str = "wikipedia"
    extract: str = ""


class ResearchFact(BaseModel):
    statement: str
    status: TruthStatus = "UNCONFIRMED"
    source_index: int | None = None

    normalize_truth = field_validator("status", mode="before")(_normalize_truth)


class ResearchNotes(BaseModel):
    summary: str
    truth_status: TruthStatus = "UNCONFIRMED"
    facts: list[ResearchFact] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    caution: str = ""
    sources: list[SourceDoc] = Field(default_factory=list)

    normalize_truth = field_validator("truth_status", mode="before")(_normalize_truth)


# ---------------------------------------------------------------- script
class ScriptDraft(BaseModel):
    title: str
    hook: str
    script: str
    ending: str = ""
    comment_prompt: str = ""
    truth_status: TruthStatus = "UNCONFIRMED"
    truth_notice: str = ""
    loop: bool = False
    # 도입 유형 (성과 비교용): question | anomaly | contradiction | number | direct_address | other
    hook_type: str = "other"
    # 정보 전개 단계 (각 단계마다 새 정보): 예) ["의문", "배경", "단서", "반전", "회수"]
    story_beats: list[str] = Field(default_factory=list)
    # 시스템이 계산해서 채우는 값 (Gemini 출력 아님)
    loop_quality: float | None = None
    loop_note: str = ""
    script_similarity: float | None = None

    normalize_truth = field_validator("truth_status", mode="before")(_normalize_truth)

    @field_validator("hook_type", mode="before")
    @classmethod
    def _hook_type(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip().lower() not in HOOK_TYPES:
            return "other"
        return value.strip().lower()


class Scene(BaseModel):
    scene_number: int
    start: float = 0.0
    end: float = 0.0
    narration: str
    narration_en: str = ""          # 해외 시청자용 영어 자막 (narration 번역)
    subtitle: str = ""
    visual: str = ""
    visual_type: Literal["video", "image"] = "video"
    visual_prompt: str = ""
    visual_search_query: str
    alt_search_queries: list[str] = Field(default_factory=list)
    effect: str = "auto"
    sfx: str | None = None
    emphasis_words: list[str] = Field(default_factory=list)

    @field_validator("visual_type", mode="before")
    @classmethod
    def _visual_type(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            if value in ("photo", "picture", "still"):
                return "image"
            if value not in ("video", "image"):
                return "video"
        return value

    @field_validator("effect", mode="before")
    @classmethod
    def _effect(cls, value: object) -> object:
        if not isinstance(value, str) or value.strip().lower() not in EFFECTS:
            return "auto"
        return value.strip().lower()

    @field_validator("sfx", mode="before")
    @classmethod
    def _sfx(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return value if value in SFX_NAMES else None
        return None


class SceneList(BaseModel):
    scenes: list[Scene]


class SceneTranslation(BaseModel):
    scene_number: int
    narration_en: str


class SceneTranslations(BaseModel):
    translations: list[SceneTranslation]


class ShortsContent(BaseModel):
    title: str
    category: str
    topic: str
    hook: str
    script: str
    scenes: list[Scene]
    ending: str = ""
    comment_prompt: str = ""
    truth_status: TruthStatus = "UNCONFIRMED"
    truth_notice: str = ""
    loop: bool = False
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)

    normalize_truth = field_validator("truth_status", mode="before")(_normalize_truth)


# -------------------------------------------------------------- metadata
class TitleCandidate(BaseModel):
    strategy: str = ""          # mystery(핵심 미스터리) | evidence(증거/발견) | paradox(역설/의문)
    title: str
    title_en: str = ""


class VideoMetadata(BaseModel):
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    # 해외 시청자용 영어 제목/설명 (설명란에 한국어와 함께 표기)
    title_en: str = ""
    description_en: str = ""
    # 서로 다른 접근의 제목 후보 (mystery | evidence | paradox) 와 선택 근거
    title_candidates: list[TitleCandidate] = Field(default_factory=list)
    title_strategy: str = ""
    title_selection: list[dict] = Field(default_factory=list)


# ----------------------------------------------------------------- media
class AssetInfo(BaseModel):
    scene_number: int
    media_type: Literal["video", "image"]
    local_path: str
    source: str
    source_id: str = ""
    source_url: str = ""
    author: str = ""
    author_url: str = ""
    license: str = ""
    downloaded_at: str = ""
    generated_by_ai: bool = False
    query: str = ""
    width: int = 0
    height: int = 0
    duration: float = 0.0


class AudioTrackInfo(BaseModel):
    file: str
    title: str = ""
    artist: str = ""
    source: str = ""
    license: str = ""
    url: str = ""
    mood: str = ""


class SubtitleChunk(BaseModel):
    index: int
    start: float
    end: float
    text: str
    highlights: list[str] = Field(default_factory=list)
    scene_number: int = 0
    text_en: str = ""               # 같은 Scene 의 영어 자막 (한국어 자막 아래 작게 표시)


class SceneTiming(BaseModel):
    scene_number: int
    start: float
    end: float
    speech_start: float
    speech_end: float
    audio_path: str
