"""config.yaml + .env 를 읽어 타입이 있는 Settings 객체로 제공한다."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"


class AppConfig(BaseModel):
    name: str = "기묘한 기록"
    mode: str = "development"
    timezone: str = "Asia/Seoul"
    language: str = "ko"
    cleanup_work_files: bool = False


class GeminiConfig(BaseModel):
    model: str
    fallback_models: list[str] = Field(default_factory=list)
    temperature: float = 0.8
    max_output_tokens: int = 8192
    max_retries: int = 3
    json_retries: int = 2
    request_timeout: int = 120
    use_google_search: bool = False


class CategoryConfig(BaseModel):
    id: str
    name: str
    examples: list[str] = Field(default_factory=list)
    guideline: str = ""
    bgm_mood: str = "dark"


class TopicConfig(BaseModel):
    candidate_count: int = 8
    max_generation_rounds: int = 3
    history_limit: int = 300
    category_strategy: str = "rotate"


class DuplicateConfig(BaseModel):
    keyword_threshold: float = 0.67
    text_threshold: float = 0.6
    semantic_min_score: float = 0.2
    semantic_check: bool = True
    semantic_top_k: int = 5


class ResearchConfig(BaseModel):
    enabled: bool = True
    provider: str = "wikipedia"
    languages: list[str] = Field(default_factory=lambda: ["ko", "en"])
    max_sources: int = 4
    max_chars_per_source: int = 3000
    user_agent: str = "StrangeRecordsBot/1.0"


class ScriptConfig(BaseModel):
    min_seconds: float = 40
    max_seconds: float = 58
    chars_per_second: float = 6.8
    min_scenes: int = 5
    max_scenes: int = 9
    banned_phrases: list[str] = Field(default_factory=list)
    # 검증 허용 범위 = 목표 범위 x 비율 (긴 대본은 음성 단계에서 말하기 속도를 올려 맞춘다)
    hard_min_ratio: float = 0.85
    hard_max_ratio: float = 1.2
    avg_sentence_chars: int = 25

    @property
    def min_chars(self) -> int:
        return int(self.min_seconds * self.chars_per_second)

    @property
    def max_chars(self) -> int:
        return int(self.max_seconds * self.chars_per_second)

    @property
    def hard_min_chars(self) -> int:
        return int(self.min_chars * self.hard_min_ratio)

    @property
    def hard_max_chars(self) -> int:
        return int(self.max_chars * self.hard_max_ratio)

    @property
    def sentence_range(self) -> tuple[int, int]:
        return max(round(self.min_chars / self.avg_sentence_chars), 1), max(round(self.max_chars / self.avg_sentence_chars), 2)


class VoiceConfig(BaseModel):
    provider: str = "edge_tts"
    language: str = "ko-KR"
    voice: str = "ko-KR-SunHiNeural"
    rate: str = "+0%"
    pitch: str = "+0Hz"
    lead_in: float = 0.2
    scene_gap: float = 0.2
    max_rate_boost: int = 20


class AIImageConfig(BaseModel):
    enabled: bool = False
    model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    style: str = ""


class AssetsConfig(BaseModel):
    providers: list[str] = Field(default_factory=lambda: ["ai_image", "pexels", "pixabay", "local", "procedural"])
    prefer_video: bool = True
    per_page: int = 15
    min_short_side: int = 720
    max_download_mb: int = 80
    request_timeout: int = 30
    ai_image: AIImageConfig = Field(default_factory=AIImageConfig)


class SubtitleConfig(BaseModel):
    engine: str = "faster_whisper"
    model_size: str = "small"
    device: str = "auto"
    compute_type: str = "int8"
    text_source: str = "script"
    max_chars: int = 11
    max_words: int = 3
    font_path: str = ""
    font_size: int = 84
    position_y: float = 0.70
    color: str = "#FFFFFF"
    highlight_color: str = "#FFD43B"
    stroke_color: str = "#000000"
    stroke_width: int = 7
    show_title: bool = True
    title_font_size: int = 64
    title_position_y: float = 0.13


class VideoConfig(BaseModel):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 20
    preset: str = "medium"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = 48000
    transition: str = "fade"
    transition_type: str = "fade"
    transition_duration: float = 0.3
    zoom_amount: float = 0.12
    outro_padding: float = 0.8
    global_fade: float = 0.4


class AudioConfig(BaseModel):
    bgm_enabled: bool = True
    bgm_volume: float = 0.18
    bgm_ducking: bool = True
    sfx_enabled: bool = True
    sfx_volume: float = 0.55
    max_sfx: int = 4
    loudnorm: bool = True
    target_lufs: float = -14


class QualityConfig(BaseModel):
    min_duration: float = 30
    max_duration: float = 60
    width: int = 1080
    height: int = 1920
    fps: float = 30
    min_file_size_kb: int = 300
    max_file_size_mb: int = 256
    max_black_ratio: float = 0.25
    black_min_duration: float = 0.5


class YouTubeConfig(BaseModel):
    privacy_status: str = "private"
    publish_mode: str = "scheduled"
    publish_time: str = "12:00"
    late_policy: str = "public_now"
    min_schedule_margin_minutes: int = 15
    category_id: str = "24"
    made_for_kids: bool = False
    default_language: str = "ko"
    contains_synthetic_media: bool = True
    client_secrets_file: str = "credentials.json"
    token_file: str = "token.json"


class ScheduleConfig(BaseModel):
    enabled: bool = True
    daily_count: int = 1
    timezone: str = "Asia/Seoul"
    generate_time: str = "10:00"
    upload_time: str = "12:00"


class RetryConfig(BaseModel):
    max_attempts: int = 3
    delays: list[tuple[float, float]] = Field(default_factory=lambda: [(5, 10), (15, 30)])


class LoggingConfig(BaseModel):
    level: str = "INFO"


class Secrets(BaseModel):
    gemini_api_key: str = ""
    pexels_api_key: str = ""
    pixabay_api_key: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""


class Paths(BaseModel):
    root: Path

    @property
    def assets(self) -> Path:
        return self.root / "assets"

    @property
    def music(self) -> Path:
        return self.assets / "music"

    @property
    def sfx(self) -> Path:
        return self.assets / "sfx"

    @property
    def local_images(self) -> Path:
        return self.assets / "images"

    @property
    def local_videos(self) -> Path:
        return self.assets / "videos"

    @property
    def fonts(self) -> Path:
        return self.assets / "fonts"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def database(self) -> Path:
        return self.data / "database.sqlite"

    @property
    def topics(self) -> Path:
        return self.data / "topics"

    @property
    def scripts(self) -> Path:
        return self.data / "scripts"

    @property
    def metadata(self) -> Path:
        return self.data / "metadata"

    @property
    def sources(self) -> Path:
        return self.data / "sources"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def output(self) -> Path:
        return self.root / "output"

    @property
    def audio(self) -> Path:
        return self.output / "audio"

    @property
    def videos(self) -> Path:
        return self.output / "videos"

    @property
    def subtitles(self) -> Path:
        return self.output / "subtitles"

    @property
    def thumbnails(self) -> Path:
        return self.output / "thumbnails"

    @property
    def work(self) -> Path:
        return self.output / "work"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def prompts(self) -> Path:
        return self.root / "app" / "ai" / "prompts"

    def ensure(self) -> None:
        for path in (
            self.music, self.sfx, self.local_images, self.local_videos, self.fonts,
            self.topics, self.scripts, self.metadata, self.sources, self.models,
            self.audio, self.videos, self.subtitles, self.thumbnails, self.work, self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    gemini: GeminiConfig
    categories: list[CategoryConfig]
    topic: TopicConfig = Field(default_factory=TopicConfig)
    duplicate: DuplicateConfig = Field(default_factory=DuplicateConfig)
    research: ResearchConfig = Field(default_factory=ResearchConfig)
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    assets: AssetsConfig = Field(default_factory=AssetsConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    secrets: Secrets = Field(default_factory=Secrets)
    paths: Paths = Field(default_factory=lambda: Paths(root=ROOT_DIR))

    @property
    def is_production(self) -> bool:
        return self.app.mode.lower() == "production"

    def category(self, category_id: str) -> CategoryConfig | None:
        for category in self.categories:
            if category.id == category_id or category.name == category_id:
                return category
        return None


def load_settings(config_path: Path | None = None, env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or ROOT_DIR / ".env", override=False)
    with open(config_path or CONFIG_PATH, encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    settings = Settings(**raw)
    settings.secrets = Secrets(
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        pexels_api_key=os.getenv("PEXELS_API_KEY", ""),
        pixabay_api_key=os.getenv("PIXABAY_API_KEY", ""),
        youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
        youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
    )
    # 운영 중 빠르게 바꿀 수 있도록 일부 값은 환경변수로 덮어쓸 수 있다.
    if os.getenv("GEMINI_MODEL"):
        settings.gemini.model = os.environ["GEMINI_MODEL"]
    if os.getenv("APP_MODE"):
        settings.app.mode = os.environ["APP_MODE"]
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
