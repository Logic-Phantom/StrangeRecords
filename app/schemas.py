"""파이프라인 전체에서 주고받는 콘텐츠 데이터 구조 (Gemini JSON 응답 검증 포함)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

TruthStatus = Literal["FACT", "UNCONFIRMED", "LEGEND", "URBAN_LEGEND", "FICTION"]
TRUTH_STATUSES: tuple[str, ...] = ("FACT", "UNCONFIRMED", "LEGEND", "URBAN_LEGEND", "FICTION")

SFX_NAMES: tuple[str, ...] = ("heartbeat", "impact", "whoosh", "suspense", "click", "transition")
EFFECTS: tuple[str, ...] = ("auto", "zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down", "static")


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

    normalize_truth = field_validator("truth_status", mode="before")(_normalize_truth)


class Scene(BaseModel):
    scene_number: int
    start: float = 0.0
    end: float = 0.0
    narration: str
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
class VideoMetadata(BaseModel):
    title: str
    description: str
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


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


class SceneTiming(BaseModel):
    scene_number: int
    start: float
    end: float
    speech_start: float
    speech_end: float
    audio_path: str
