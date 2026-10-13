"""Gemini 로 제목/설명/해시태그를 생성하고, 출처 표기는 시스템이 정확하게 덧붙인다."""

from __future__ import annotations

import re

from app.ai.gemini_client import ContentValidationError, GeminiClient, load_prompt
from app.config.settings import Settings
from app.schemas import AssetInfo, AudioTrackInfo, ResearchNotes, ShortsContent, VideoMetadata
from app.utils.logger import get_logger

logger = get_logger("metadata")

TRUTH_LABELS = {
    "FACT": "기록으로 확인된 사실을 바탕으로 합니다.",
    "UNCONFIRMED": "확인되지 않은 내용이 포함되어 있습니다.",
    "LEGEND": "전설로 전해지는 이야기입니다.",
    "URBAN_LEGEND": "도시전설/괴담으로 전해지는 이야기이며 사실로 확인되지 않았습니다.",
    "FICTION": "창작된 이야기입니다.",
}

TRUTH_LABELS_EN = {
    "FACT": "Based on documented facts.",
    "UNCONFIRMED": "Contains unconfirmed information.",
    "LEGEND": "This is a legend that has been passed down.",
    "URBAN_LEGEND": "This is an urban legend and has not been confirmed as fact.",
    "FICTION": "This is a fictional story.",
}

SOURCE_LABELS = {
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "local": "채널 보유 자료",
    "ai_image": "AI 생성 이미지(Gemini)",
    "hf_image": "AI 생성 이미지(Stable Diffusion XL)",
    "cf_image": "AI 생성 이미지(Stable Diffusion XL)",
    "procedural": "자체 제작 그래픽",
}


def _clean_hashtag(tag: str) -> str:
    tag = re.sub(r"\s+", "", tag.strip())
    if not tag:
        return ""
    return tag if tag.startswith("#") else f"#{tag}"


def build_hashtags(raw: list[str], limit: int = 8) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    for tag in raw:
        cleaned = _clean_hashtag(tag)
        if cleaned and cleaned.lower() not in seen and cleaned.lower() != "#shorts":
            tags.append(cleaned)
            seen.add(cleaned.lower())
    return tags[: limit - 1] + ["#Shorts"]


def build_credits(research: ResearchNotes | None, assets: list[AssetInfo], bgm: AudioTrackInfo | None) -> str:
    lines: list[str] = []
    if research and research.sources:
        lines.append("📚 자료 출처 (Sources)")
        for src in research.sources:
            lines.append(f"- {src.title}" + (f" ({src.url})" if src.url else ""))
    if assets:
        lines.append("🎞 영상/이미지 출처 (Visuals)")
        seen: set[str] = set()
        for a in sorted(assets, key=lambda x: x.scene_number):
            label = SOURCE_LABELS.get(a.source, a.source)
            by = f" by {a.author}" if a.author else ""
            url = f" {a.source_url}" if a.source_url else ""
            line = f"- {label}{by}{url}"
            if line not in seen:
                seen.add(line)
                lines.append(line)
    if bgm:
        lines.append("🎵 음악 (Music)")
        lines.append(f"- {bgm.title or bgm.file}" + (f" / {bgm.artist}" if bgm.artist else "") + (f" ({bgm.license})" if bgm.license else ""))
    if any(a.generated_by_ai for a in assets):
        lines.append("※ 일부 이미지는 AI 로 생성되었습니다. (Some images are AI-generated.)")
    lines.append("※ 이 영상의 대본과 음성은 AI(Gemini, Edge TTS)를 활용해 제작되었습니다."
                 " (Script and voice were produced with AI: Gemini, Edge TTS.)")
    return "\n".join(lines)


def build_english_section(title_en: str, description_en: str, truth_status: str) -> str:
    """해외 시청자용 영어 제목/설명 블록. 영어 설명이 없으면 빈 문자열."""
    if not description_en.strip():
        return ""
    lines = ["🌏 English"]
    if title_en.strip():
        lines.append(title_en.strip())
    lines.append(description_en.strip())
    if truth_status != "FACT":
        lines.append(f"ℹ️ {TRUTH_LABELS_EN.get(truth_status, '')}".rstrip())
    return "\n".join(lines)


def compose_description(
    body: str, truth_status: str, truth_notice: str, credits: str, hashtags: list[str], english: str = ""
) -> str:
    parts = [body.strip()]
    notice = truth_notice.strip() or (TRUTH_LABELS.get(truth_status, "") if truth_status != "FACT" else "")
    if notice:
        parts.append(f"ℹ️ {notice}")
    parts.append(english)
    parts.append(credits)
    parts.append(" ".join(hashtags))
    description = "\n\n".join(p for p in parts if p)
    # YouTube 설명은 5000자 제한, '<' '>' 는 허용되지 않음
    return description.replace("<", "‹").replace(">", "›")[:4900]


def fit_tags(tags: list[str], limit_chars: int = 450) -> list[str]:
    out: list[str] = []
    total = 0
    for tag in tags:
        tag = tag.strip().lstrip("#").replace("<", "").replace(">", "")
        if not tag or tag in out:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + 1
        if total + cost > limit_chars:
            break
        out.append(tag)
        total += cost
    return out


class MetadataGenerator:
    def __init__(self, client: GeminiClient, settings: Settings):
        self.client = client
        self.settings = settings

    def generate(
        self,
        content: ShortsContent,
        research: ResearchNotes | None,
        assets: list[AssetInfo],
        bgm: AudioTrackInfo | None,
    ) -> VideoMetadata:
        category = self.settings.category(content.category)
        prompt = load_prompt(
            "metadata_prompt",
            channel_name=self.settings.app.name,
            category_name=category.name if category else content.category,
            topic=content.topic,
            truth_status=content.truth_status,
            truth_notice=content.truth_notice or "(없음)",
            draft_title=content.title,
            script=content.script,
        )

        def _validate(meta: VideoMetadata) -> None:
            if not meta.title.strip():
                raise ContentValidationError("title 이 비어 있다.")
            if len(meta.title) > 90:
                raise ContentValidationError(f"title 이 너무 길다 ({len(meta.title)}자). 40자 이내로 줄여라.")
            if not meta.description.strip():
                raise ContentValidationError("description 이 비어 있다.")
            if not meta.description_en.strip() or not meta.title_en.strip():
                raise ContentValidationError("title_en / description_en(영어 제목/설명)을 작성해야 한다.")
            if re.search(r"[가-힣]", meta.title_en + meta.description_en):
                raise ContentValidationError("title_en / description_en 은 영어로만 써야 한다 (한글 금지).")

        meta = self.client.generate_json(prompt, VideoMetadata, validate=_validate, temperature=0.7)
        return finalize_metadata(meta, content, research, assets, bgm)


def finalize_metadata(
    meta: VideoMetadata,
    content: ShortsContent,
    research: ResearchNotes | None,
    assets: list[AssetInfo],
    bgm: AudioTrackInfo | None,
) -> VideoMetadata:
    hashtags = build_hashtags(meta.hashtags)
    credits = build_credits(research, assets, bgm)
    title = meta.title.strip().replace("<", "").replace(">", "")[:100]
    english = build_english_section(meta.title_en, meta.description_en, content.truth_status)
    description = compose_description(
        meta.description, content.truth_status, content.truth_notice, credits, hashtags, english=english
    )
    tags = fit_tags(meta.tags + [h.lstrip("#") for h in hashtags])
    logger.info("Metadata generated: %s / %s", title, meta.title_en or "-")
    return VideoMetadata(
        title=title, description=description, hashtags=hashtags, tags=tags,
        title_en=meta.title_en.strip(), description_en=meta.description_en.strip(),
    )
