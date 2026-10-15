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
    # rotate(가장 오래 안 쓴 카테고리) | random
    # | rotate_performance(최근 diversity_window 개 카테고리는 제외하고, 오래 안 쓴 순서 + 채널 성과로 선택. 성과 표본이 부족하면 rotate 와 같다)
    category_strategy: str = "rotate"
    diversity_window: int = 2


class YouTubeTrendConfig(BaseModel):
    enabled: bool = True
    most_popular: bool = True        # videos.list chart=mostPopular (1 unit)
    max_results: int = 25
    search_per_run: int = 3          # search.list 는 요청당 100 units → 후보 상위 N 개만 조회
    search_max_results: int = 15
    recent_days: int = 30            # 이 기간 안에 올라온 영상만 과밀도 계산에 사용


class WikipediaTrendConfig(BaseModel):
    enabled: bool = True
    days: int = 30                   # 일별 조회수 조회 기간 (최근 7일 vs 이전 기간 → 증가 속도)
    top_articles: bool = True        # ko 위키백과 일간 인기 문서 → 주제 힌트
    target_daily_views: float = 3000  # 이 일평균 조회수면 관심도 1.0 (log 정규화)
    min_interval: float = 1.0        # 위키백과 요청 사이 최소 간격(초). 연속 요청 시 429 → 자료 조사 단계까지 막히지 않도록
    max_candidates: int = 5          # 후보 신호를 조회할 최대 후보 수 (덜 비슷한 후보부터)


class GoogleTrendsConfig(BaseModel):
    # 공식 Google Trends API 는 2026-10 현재 알파(신청자만, 엔드포인트 비공개) → 접근 권한을 받은 뒤에만 켠다
    enabled: bool = False
    endpoint: str = ""


class TrendScoringConfig(BaseModel):
    # 모든 구성 요소는 0~1 로 정규화한 뒤 가중 평균한다 (값이 없는 요소는 가중치에서 빠지고 나머지로 다시 정규화)
    weights: dict[str, float] = Field(default_factory=lambda: {
        "trend": 0.15, "velocity": 0.10, "channel_fit": 0.15, "performance": 0.10,
        "novelty": 0.20, "story": 0.15, "evidence": 0.15,
    })
    saturation_penalty: float = 0.15
    duplicate_penalty: float = 0.20
    min_channel_fit: float = 0.35          # 이보다 채널 관련성이 낮으면 외부 관심도(trend/velocity)를 반영하지 않음
    min_evidence_for_trend: float = 0.5    # 사실 확인 가능성이 낮은 후보(FICTION 등)는 외부 관심도를 반영하지 않음
    freshness_half_life_hours: float = 48  # 신호 나이가 이만큼이면 가중치 절반


class TrendConfig(BaseModel):
    enabled: bool = True
    region: str = "KR"
    language: str = "ko"
    max_hints: int = 6                     # 주제 프롬프트에 넣을 채널 관련 트렌드 힌트 수
    request_timeout: int = 15
    channel_keywords: list[str] = Field(default_factory=list)
    # 이 단어가 들어간 화제는 힌트로 쓰지 않는다 (실제 피해자가 있는 강력 범죄/참사/정치 사건 등)
    exclude_keywords: list[str] = Field(default_factory=lambda: [
        "살인", "학대", "성폭", "저격", "피살", "참사", "학살", "자살", "테러", "사망 사고", "선거", "대통령",
    ])
    youtube: YouTubeTrendConfig = Field(default_factory=YouTubeTrendConfig)
    wikipedia: WikipediaTrendConfig = Field(default_factory=WikipediaTrendConfig)
    google_trends: GoogleTrendsConfig = Field(default_factory=GoogleTrendsConfig)
    scoring: TrendScoringConfig = Field(default_factory=TrendScoringConfig)


class AnalyticsConfig(BaseModel):
    enabled: bool = True
    lookback_days: int = 90              # 게시 후 이 기간 안의 영상만 수집
    max_videos_per_run: int = 30
    checkpoints_hours: list[float] = Field(default_factory=lambda: [24, 72, 168])
    min_hours_for_judgement: float = 48  # 게시 후 이 시간 전에는 성과 판단 보류
    min_samples: int = 3                 # 그룹 비교에 필요한 최소 영상 수 (미만이면 "판단 불가")
    shrinkage_k: float = 5               # 작은 표본을 채널 평균 쪽으로 당기는 강도
    metrics: list[str] = Field(default_factory=lambda: [
        "views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration", "averageViewPercentage",
        "likes", "comments", "shares", "subscribersGained", "subscribersLost",
    ])
    request_timeout: int = 30


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


class LoopEndingConfig(BaseModel):
    enabled: bool = True
    mode: str = "natural"                    # natural: 이야기에 맞을 때만 / off
    require_semantic_continuity: bool = True
    minimum_quality_score: float = 0.75      # 0~1, 이보다 낮으면 루프를 쓰지 않고 일반 결말로 기록


class ScriptQualityConfig(BaseModel):
    compare_recent: int = 30                 # 최근 N개 대본과 비교
    max_script_similarity: float = 0.55      # 문자 bigram 유사도가 이 이상이면 재생성
    max_hook_similarity: float = 0.7         # 첫 문장이 최근 영상과 이만큼 비슷하면 재생성
    max_hook_seconds: float = 6.0            # 첫 문장 낭독 시간 상한 (핵심 정보는 첫 3초 안)
    max_sentence_repeat: float = 0.85        # 대본 안 두 문장이 이만큼 비슷하면 불필요한 반복


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
    loop_ending: LoopEndingConfig = Field(default_factory=LoopEndingConfig)
    quality: ScriptQualityConfig = Field(default_factory=ScriptQualityConfig)

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
    # Edge TTS 접속 주소 (앞에서부터 시도, 403 이면 다음). 이름(msedgeservices | bing) 또는 wss:// URL
    endpoints: list[str] = Field(default_factory=lambda: ["msedgeservices", "bing"])
    min_interval: float = 2.5        # 요청 사이 최소 간격(초)
    rate_limit_waits: list[float] = Field(default_factory=lambda: [5, 10, 20, 30, 45])  # 429 시 대기(초)


class AIImageConfig(BaseModel):
    enabled: bool = False
    model: str = ""
    fallback_models: list[str] = Field(default_factory=list)
    style: str = ""


class HFImageConfig(BaseModel):
    enabled: bool = False
    model: str = "stabilityai/stable-diffusion-xl-base-1.0"
    provider: str = "auto"
    width: int = 768
    height: int = 1344
    num_inference_steps: int = 30
    guidance_scale: float = 7.0
    negative_prompt: str = ""
    timeout: int = 120


class CFImageConfig(BaseModel):
    enabled: bool = False
    model: str = "@cf/stabilityai/stable-diffusion-xl-base-1.0"
    fallback_models: list[str] = Field(default_factory=list)
    width: int = 768
    height: int = 1344
    num_steps: int = 20
    guidance: float = 7.5
    negative_prompt: str = ""
    timeout: int = 120


class AssetsConfig(BaseModel):
    providers: list[str] = Field(
        default_factory=lambda: ["ai_image", "cf_image", "hf_image", "pexels", "pixabay", "local", "procedural"]
    )
    prefer_video: bool = True
    per_page: int = 15
    min_short_side: int = 720
    max_download_mb: int = 80
    request_timeout: int = 30
    # true: 모든 Scene 에 대본 내용대로 그린 AI 이미지가 있어야 한다 (자체 그래픽/어두운 화면으로 채우지 않음).
    #       AI 이미지가 빠진 Scene 은 ai_retry_waits 만큼 쉬었다가 다시 그리고, 그래도 없으면 단계를 실패시켜 나중에 재시도
    require_ai_image: bool = False
    ai_retry_waits: list[float] = Field(default_factory=lambda: [30, 90, 180])
    ai_image: AIImageConfig = Field(default_factory=AIImageConfig)
    hf_image: HFImageConfig = Field(default_factory=HFImageConfig)
    cf_image: CFImageConfig = Field(default_factory=CFImageConfig)


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
    # 영어 자막 (Scene 번역문을 한국어 자막 아래 작게 표시)
    english: bool = True
    en_font_size: int = 38
    en_min_font_size: int = 30
    en_max_lines: int = 3
    en_position_y: float = 0.69      # 영어 자막 블록의 위쪽 끝 (화면 높이 비율)
    en_color: str = "#E6E6E6"
    en_stroke_width: int = 4


class HookEffectConfig(BaseModel):
    """첫 장면 앞부분에만 거는 시각 효과 (자막 합성 전 클립에 적용 → 자막은 흔들리거나 번지지 않는다)."""

    enabled: bool = False
    duration_seconds: float = 2.5
    preset: str = "subtle"                   # subtle | vhs | glitch | impact | none
    allow_glitch: bool = True
    allow_noise: bool = True
    allow_shake: bool = True
    deterministic_seed: bool = True          # 같은 작업은 같은 흔들림/글리치 위치


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
    hook_effect: HookEffectConfig = Field(default_factory=HookEffectConfig)


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


class ReviewConfig(BaseModel):
    """업로드 전 콘텐츠 검수 (사실/출처/독창성/제목/라이선스). critical 문제가 있으면 업로드하지 않는다."""

    enabled: bool = True
    require_sources_for: list[str] = Field(default_factory=lambda: ["FACT", "UNCONFIRMED"])
    max_script_similarity: float = 0.6       # 최근 영상 대본과 이 이상 비슷하면 실질적으로 같은 구성 → critical
    max_title_similarity: float = 0.75       # 최근 제목과 이 이상 비슷하면 경고
    template_window: int = 5                 # 최근 N개와 도입 유형+첫 구절이 모두 같으면 반복 템플릿 경고
    max_title_emoji: int = 1
    min_ending_chars: int = 8
    max_fix_attempts: int = 1                # 제목 문제 등 가벼운 문제의 자동 수정 횟수


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
    hf_api_key: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    # 선택: 트렌드 탐색용 YouTube Data API Key (없으면 token.json 의 OAuth 인증으로 조회)
    youtube_api_key: str = ""
    # 선택: Google Trends 공식 API 알파 접근 권한을 받은 경우에만
    google_trends_api_key: str = ""


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

    @property
    def reports(self) -> Path:
        return self.data / "reports"

    def ensure(self) -> None:
        for path in (
            self.music, self.sfx, self.local_images, self.local_videos, self.fonts,
            self.topics, self.scripts, self.metadata, self.sources, self.models,
            self.audio, self.videos, self.subtitles, self.thumbnails, self.work, self.logs, self.reports,
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
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    trends: TrendConfig = Field(default_factory=TrendConfig)
    analytics: AnalyticsConfig = Field(default_factory=AnalyticsConfig)
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
        hf_api_key=os.getenv("HF_API_KEY") or os.getenv("HF_TOKEN", ""),
        cloudflare_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
        cloudflare_api_token=os.getenv("CLOUDFLARE_API_TOKEN", ""),
        youtube_client_id=os.getenv("YOUTUBE_CLIENT_ID", ""),
        youtube_client_secret=os.getenv("YOUTUBE_CLIENT_SECRET", ""),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
        google_trends_api_key=os.getenv("GOOGLE_TRENDS_API_KEY", ""),
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
