"""Daily Pipeline

[1] DB 기존 주제 조회 → [2] Gemini 주제 후보 → [3] 중복 검사 → [4] 최종 주제
→ [5] 자료 조사 → [6] Gemini 대본 → [7] 대본 검증 → [8] Scene 생성
→ [9] Edge TTS 음성 (Scene 별 실제 길이 확정) → [10] Pexels/Pixabay 자료 수집
→ [11] Whisper 자막 → [12] FFmpeg 영상(첫 장면 Hook 효과 선택) → [13] 품질 검사
→ [14] Gemini 제목 후보/설명/해시태그 → [14-1] 콘텐츠 검수(출처/독창성/라이선스, critical 이면 업로드 금지)
→ [15] YouTube 업로드 + [16] 12:00 예약 공개 → [17] SQLite 기록 + 제작 조건(production_metadata)

주제 단계는 외부 트렌드 신호와 채널 성과(Analytics)를 참고해 점수로 고르고, 둘 다 없으면 기존 방식으로 고른다.

각 단계 결과는 DB(jobs.state_json)와 data/, output/ 에 저장되므로
실패한 작업은 `python -m app.main retry` 로 실패한 단계부터 다시 실행된다.
"""

from __future__ import annotations

import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.ai.gemini_client import GeminiClient
from app.ai.metadata_generator import MetadataGenerator, finalize_metadata
from app.ai.scene_generator import SceneGenerator, missing_english, renumber, translate_scenes
from app.ai.script_generator import ScriptGenerator
from app.ai.topic_generator import TopicGenerator
from app.assets.asset_manager import AssetManager, build_providers
from app.assets.audio_library import AudioLibrary
from app.config.settings import Settings
from app.database.db import Database
from app.database.models import JobRecord, JobStatus, TopicStatus
from app.database.repositories import (
    AssetRepository, JobRepository, ProductionMetadataRepository, StepLogRepository, TopicRepository,
    TopicScoreRepository, TrendRepository,
)
from app.quality.checker import QualityChecker
from app.quality.content_checker import fix_title, review_content
from app.research.researcher import Researcher
from app.schemas import (
    AssetInfo, AudioTrackInfo, ResearchNotes, SceneTiming, ScriptDraft, ShortsContent, SubtitleChunk,
    TopicCandidate, VideoMetadata,
)
from app.subtitle.chunker import build_subtitles, to_srt
from app.subtitle.whisper import Word, WhisperTranscriber
from app.topics.duplicate_checker import DuplicateChecker
from app.topics.selector import TopicSelector
from app.utils.files import new_job_id, now_iso, read_json, remove_dir, write_json
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError, retry_call
from app.video.renderer import VideoRenderer
from app.voice.edge_tts import create_voice_provider
from app.voice.narration import build_narration
from app.youtube.uploader import YouTubeUploader, plan_publish

logger = get_logger("pipeline")

STEPS = [
    "topic", "research", "script", "scenes", "voice", "assets", "subtitles", "render", "quality", "metadata", "review",
    "upload", "record",
]


class QualityCheckFailed(NonRetryableError):
    pass


class ContentReviewFailed(NonRetryableError):
    """콘텐츠 검수 critical 문제 → 업로드 금지, 작업은 review_needed 로 저장."""


@dataclass
class RunOptions:
    upload: bool = False
    offline: bool = False                 # Gemini 없이 fixture 대본으로 제작 (test 전용)
    fixture: Path | None = None
    forced_topic: str | None = None
    forced_category: str | None = None
    mode_label: str = "development"


@dataclass
class JobContext:
    job: JobRecord
    state: dict[str, Any] = field(default_factory=dict)
    completed: list[str] = field(default_factory=list)
    current: str | None = None

    @property
    def id(self) -> str:
        return self.job.id


class DailyPipeline:
    def __init__(self, settings: Settings, db: Database | None = None):
        self.settings = settings
        self.paths = settings.paths
        self.paths.ensure()
        self.db = db or Database(self.paths.database)
        tz = settings.app.timezone
        self.topics = TopicRepository(self.db, tz)
        self.jobs = JobRepository(self.db, tz)
        self.assets_repo = AssetRepository(self.db)
        self.step_logs = StepLogRepository(self.db, tz)
        self.score_repo = TopicScoreRepository(self.db, tz)
        self.prod_repo = ProductionMetadataRepository(self.db, tz)
        self._gemini: GeminiClient | None = None
        self._insights = None
        self.options = RunOptions()

    # ----------------------------------------------------------- helpers
    @property
    def gemini(self) -> GeminiClient:
        if self._gemini is None:
            self._gemini = GeminiClient(self.settings.gemini, self.settings.secrets.gemini_api_key, self.settings.retry)
        return self._gemini

    def work_dir(self, ctx: JobContext) -> Path:
        return self.paths.work / ctx.id

    def _save(self, ctx: JobContext, current: str | None = None) -> None:
        self.jobs.save_state(ctx.id, ctx.completed, ctx.state, current)

    def _content(self, ctx: JobContext) -> ShortsContent:
        return ShortsContent(**ctx.state["content"])

    def _candidate(self, ctx: JobContext) -> TopicCandidate:
        return TopicCandidate(**ctx.state["topic"])

    def _research(self, ctx: JobContext) -> ResearchNotes | None:
        data = ctx.state.get("research")
        return ResearchNotes(**data) if data else None

    def _assets(self, ctx: JobContext) -> list[AssetInfo]:
        return [AssetInfo(**a) for a in ctx.state.get("assets", [])]

    def _timings(self, ctx: JobContext) -> list[SceneTiming]:
        return [SceneTiming(**t) for t in ctx.state["narration"]["timings"]]

    def _bgm(self, ctx: JobContext) -> AudioTrackInfo | None:
        data = ctx.state.get("render", {}).get("bgm")
        return AudioTrackInfo(**data) if data else None

    def _mood(self, ctx: JobContext) -> str:
        category = self.settings.category(ctx.state.get("topic", {}).get("category", ""))
        return category.bgm_mood if category else "dark"

    @property
    def insights(self):
        """채널 성과 요약 (Analytics 수집 결과). 없거나 실패하면 반영하지 않는다."""
        if self._insights is None and not self.options.offline and self.settings.analytics.enabled:
            from app.analytics.insights import PerformanceInsights

            self._insights = PerformanceInsights(self.db, self.settings)
        return self._insights

    def _performance_context(self, category: str) -> str:
        try:
            return self.insights.prompt_context(category) if self.insights else ""
        except Exception as exc:
            logger.warning("성과 요약 실패 → 프롬프트에 넣지 않음: %s", exc)
            return ""

    def _previous(self, ctx: JobContext) -> list[dict]:
        """최근 제작물 (대본/제목/도입 유사도 비교용). offline 샘플과 현재 작업은 제외."""
        limit = self.settings.script.quality.compare_recent
        out: list[dict] = []
        for job in self.jobs.list(limit + 20):
            state = job.state or {}
            if job.id == ctx.id or state.get("options", {}).get("offline"):
                continue
            content, draft = state.get("content") or {}, state.get("draft") or {}
            script = content.get("script") or draft.get("script")
            if not script:
                continue
            out.append({
                "job_id": job.id,
                "title": (state.get("metadata") or {}).get("title") or content.get("title") or draft.get("title", ""),
                "hook": content.get("hook") or draft.get("hook", ""),
                "script": script,
                "hook_type": draft.get("hook_type", ""),
            })
        return out[:limit]

    # --------------------------------------------------------------- run
    def run_new(self, options: RunOptions) -> JobContext:
        self.options = options
        job_id = new_job_id(self.settings.app.timezone)
        while self.jobs.get(job_id):
            time.sleep(1)
            job_id = new_job_id(self.settings.app.timezone)
        job = self.jobs.create(job_id, options.mode_label, options.upload)
        ctx = JobContext(job=job, state={"options": {"offline": options.offline, "fixture": str(options.fixture) if options.fixture else None}})
        logger.info("=" * 60)
        logger.info("Daily pipeline started (job=%s, mode=%s, upload=%s)", job_id, options.mode_label, options.upload)
        return self._run(ctx)

    def resume(self, job_id: str, upload: bool | None = None, only_until: str | None = None) -> JobContext:
        job = self.jobs.get(job_id)
        if not job:
            raise ValueError(f"작업 없음: {job_id}")
        upload_enabled = job.upload_enabled if upload is None else upload
        opts = job.state.get("options", {})
        self.options = RunOptions(
            upload=upload_enabled,
            offline=bool(opts.get("offline")),
            fixture=Path(opts["fixture"]) if opts.get("fixture") else None,
            mode_label=job.mode,
        )
        if upload is not None and upload != job.upload_enabled:
            self.jobs.update(job_id, upload_enabled=int(upload))
            job.upload_enabled = upload
        self.jobs.update(job_id, status=JobStatus.RUNNING, error=None, retry_count=job.retry_count + 1)
        ctx = JobContext(job=job, state=job.state, completed=list(job.completed_steps))
        # 업로드 활성화로 재실행 시 upload/record 단계를 다시 수행
        if upload_enabled:
            ctx.completed = [s for s in ctx.completed if s not in ("upload", "record")]
        logger.info("=" * 60)
        logger.info("Resume job %s (완료 단계: %s)", job_id, ", ".join(ctx.completed) or "-")
        return self._run(ctx)

    def _run(self, ctx: JobContext) -> JobContext:
        started = time.time()
        try:
            for step in STEPS:
                if step in ctx.completed:
                    continue
                if step == "upload" and not ctx.job.upload_enabled:
                    logger.info("[upload] 건너뜀 (업로드 비활성: development/test 모드)")
                    continue
                self._run_step(ctx, step, getattr(self, f"step_{step}"))
        except Exception as exc:
            self._fail(ctx, exc)
            raise
        logger.info("Daily pipeline finished: job=%s (%.0f초)", ctx.id, time.time() - started)
        return ctx

    def _run_step(self, ctx: JobContext, step: str, func: Callable[[JobContext], None]) -> None:
        t0 = time.time()
        logger.info("[%s] 시작", step)
        ctx.current = step
        self._save(ctx, step)
        func(ctx)
        ctx.completed.append(step)
        self._save(ctx, None)
        elapsed = time.time() - t0
        self.step_logs.add(ctx.id, step, "success", duration_s=round(elapsed, 2))
        logger.info("[%s] 완료 (%.1f초)", step, elapsed)

    def _fail(self, ctx: JobContext, exc: Exception) -> None:
        step = ctx.current or "?"
        logger.error("Pipeline FAILED | STEP=%s | EXCEPTION=%s: %s", step, type(exc).__name__, exc)
        logger.debug(traceback.format_exc())
        status = JobStatus.REVIEW_NEEDED if isinstance(exc, ContentReviewFailed) else JobStatus.FAILED
        self.jobs.update(ctx.id, status=status, error=f"[{step}] {type(exc).__name__}: {exc}"[:2000])
        self.step_logs.add(ctx.id, str(step), "failed", f"{type(exc).__name__}: {exc}")
        if ctx.state.get("topic_id"):
            self.topics.update(ctx.state["topic_id"], status=TopicStatus.FAILED)

    # ============================================================ STEPS
    def _fixture(self) -> dict:
        path = self.options.fixture or (self.paths.root / "tests" / "fixtures" / "sample_content.json")
        data = read_json(path)
        if not data:
            raise NonRetryableError(f"offline fixture 없음: {path}")
        return data

    def step_topic(self, ctx: JobContext) -> None:
        if self.options.offline:
            fx = self._fixture()
            candidate = TopicCandidate(**fx["topic"])
            ctx.state["topic"] = candidate.model_dump()
            write_json(self.paths.topics / f"{ctx.id}.json", {"selected": candidate.model_dump(), "offline": True})
            return

        if self.options.forced_topic:
            category = self.settings.category(self.options.forced_category or "") or self.settings.categories[0]
            candidate = TopicCandidate(
                title=self.options.forced_topic, topic=self.options.forced_topic, category=category.id,
                search_terms_ko=[self.options.forced_topic],
            )
            selection = {"selected": candidate.model_dump(), "forced": True}
        else:
            trends = None
            if self.settings.trends.enabled:
                try:
                    from app.trends.collector import TrendCollector

                    trends = TrendCollector(self.settings, TrendRepository(self.db))
                except Exception as exc:  # 외부 신호는 보조 기능
                    logger.warning("트렌드 수집기 준비 실패 → 기존 방식으로 주제 선정: %s", exc)
            selector = TopicSelector(
                self.settings,
                TopicGenerator(self.gemini, self.settings),
                DuplicateChecker(self.settings.duplicate, self.gemini),
                self.topics,
                trends=trends,
                insights=self.insights,
            )
            result = selector.select(self.options.forced_category)
            candidate = result.selected
            selection = result.to_dict()
            sel = result.selected_score
            ctx.state["topic_selection"] = {
                "strategy": result.strategy, "score": round(sel.final, 4) if sel else None,
                "trend_sources": sel.trend_sources if sel else [], "reasons": sel.reasons if sel else [],
                "category_reason": result.category_reason, "trend_status": result.trend_status,
            }
            try:
                self.score_repo.add_many(ctx.id, [s.to_row() for s in result.scores], result.strategy)
            except Exception as exc:
                logger.warning("주제 점수 기록 실패: %s", exc)

        topic_id = self.topics.add(candidate, job_id=ctx.id, status=TopicStatus.SELECTED)
        self.jobs.update(ctx.id, topic_id=topic_id)
        ctx.state["topic"] = candidate.model_dump()
        ctx.state["topic_id"] = topic_id
        write_json(self.paths.topics / f"{ctx.id}.json", selection)

    def step_research(self, ctx: JobContext) -> None:
        candidate = self._candidate(ctx)
        if self.options.offline:
            fx = self._fixture()
            notes = ResearchNotes(**fx["research"]) if fx.get("research") else None
        else:
            notes = Researcher(self.settings, self.gemini).research(candidate)
        ctx.state["research"] = notes.model_dump() if notes else None
        write_json(self.paths.sources / f"{ctx.id}.json", ctx.state["research"] or {})

    def step_script(self, ctx: JobContext) -> None:
        if self.options.offline:
            fx = self._fixture()["content"]
            draft = ScriptDraft(**{k: fx[k] for k in ScriptDraft.model_fields if k in fx})
        else:
            candidate = self._candidate(ctx)
            draft = ScriptGenerator(self.gemini, self.settings).generate(
                candidate, self._research(ctx), previous=self._previous(ctx),
                performance_context=self._performance_context(candidate.category),
            )
        ctx.state["draft"] = draft.model_dump()

    def step_scenes(self, ctx: JobContext) -> None:
        draft = ScriptDraft(**ctx.state["draft"])
        candidate = self._candidate(ctx)
        if self.options.offline:
            from app.schemas import Scene

            scenes = renumber([Scene(**s) for s in self._fixture()["content"]["scenes"]])
        else:
            scenes = SceneGenerator(self.gemini, self.settings).generate(draft, candidate)
        content = ShortsContent(
            title=draft.title, category=candidate.category, topic=candidate.topic, hook=draft.hook,
            # 실제로 읽는 문장은 Scene narration 이므로 script 를 그것으로 맞춘다
            script=" ".join(s.narration.strip() for s in scenes),
            scenes=scenes, ending=draft.ending, comment_prompt=draft.comment_prompt,
            truth_status=draft.truth_status, truth_notice=draft.truth_notice, loop=draft.loop,
        )
        ctx.state["content"] = content.model_dump()
        write_json(self.paths.scripts / f"{ctx.id}.json", ctx.state["content"])

    def step_voice(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        provider = create_voice_provider(self.settings.voice, self.settings.retry)
        result = build_narration(content.scenes, provider, self.settings, self.paths.audio / ctx.id)
        ctx.state["narration"] = {
            "path": str(result.path), "duration": result.duration, "rate": result.rate,
            "timings": [t.model_dump() for t in result.timings],
        }
        # Scene 의 start/end 를 실제 음성 기준으로 갱신
        timing_map = {t.scene_number: t for t in result.timings}
        for scene in content.scenes:
            scene.start, scene.end = timing_map[scene.scene_number].start, timing_map[scene.scene_number].end
        ctx.state["content"] = content.model_dump()
        write_json(self.paths.scripts / f"{ctx.id}.json", ctx.state["content"])

    def step_assets(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        used: set[str] = set()
        for source in ("pexels", "pixabay"):
            used |= self.assets_repo.used_source_ids(source)  # 다른 영상에서 쓴 자료 재사용 방지
        # AI 이미지는 Gemini Key 가 있을 때만 (offline 테스트는 Gemini / Hugging Face 를 호출하지 않음)
        use_ai = self.settings.assets.ai_image.enabled and bool(self.settings.secrets.gemini_api_key) and not self.options.offline
        providers = build_providers(self.settings, self.gemini if use_ai else None, ai_fallback=not self.options.offline)
        # AI 이미지 필수(offline 테스트 제외): 빠진 Scene 이 있으면 자체 그래픽 대신 단계를 실패시켜 나중에 이어서 그린다
        require_ai = self.settings.assets.require_ai_image and not self.options.offline
        manager = AssetManager(self.settings, providers, used_ids=used, require_ai=require_ai)
        partial_file = self.paths.sources / f"{ctx.id}_assets_partial.json"
        existing = [AssetInfo(**a) for a in (read_json(partial_file) or [])]
        assets = manager.collect(
            content.scenes, self.work_dir(ctx) / "assets", self._mood(ctx), self._timings(ctx),
            context=f"{content.title} — {content.topic}", existing=existing,
            on_progress=lambda done: write_json(partial_file, [a.model_dump() for a in done]),
        )
        ctx.state["assets"] = [a.model_dump() for a in assets]
        self.assets_repo.replace_for_job(ctx.id, assets)
        write_json(self.paths.sources / f"{ctx.id}_assets.json", ctx.state["assets"])

    def _ensure_english(self, ctx: JobContext, content: ShortsContent) -> None:
        """영어 자막이 빠진 Scene 을 번역해 채운다. 실패해도 한국어 자막만으로 계속 제작한다."""
        if not self.settings.subtitle.english or self.options.offline or not missing_english(content.scenes):
            return
        try:
            translate_scenes(self.gemini, content.scenes, content.topic)
        except Exception as exc:
            logger.warning("영어 자막 번역 실패 → 한국어 자막만 사용: %s", exc)
            return
        ctx.state["content"] = content.model_dump()
        write_json(self.paths.scripts / f"{ctx.id}.json", ctx.state["content"])

    def step_subtitles(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        self._ensure_english(ctx, content)
        narration = Path(ctx.state["narration"]["path"])
        words_file = self.paths.subtitles / f"{ctx.id}_words.json"

        def _transcribe() -> list[Word]:
            transcriber = WhisperTranscriber(self.settings.subtitle, self.paths.models)
            return transcriber.transcribe(narration, language=self.settings.app.language)

        try:
            words = retry_call(_transcribe, step="Whisper", max_attempts=2, delays=self.settings.retry.delays)
        except Exception as exc:
            if self.settings.subtitle.text_source != "script":
                raise
            # 대본 텍스트를 쓰는 모드에서는 Scene 음성 길이 비율로 자막 타이밍을 계산해 계속 진행
            logger.warning("Whisper 실패 → Scene 음성 길이 기준 자막 타이밍 사용: %s", exc)
            words = []
        write_json(words_file, [w.to_dict() for w in words])

        sub = self.settings.subtitle
        chunks = build_subtitles(
            content.scenes, self._timings(ctx), words,
            text_source=sub.text_source, max_chars=sub.max_chars, max_words=sub.max_words, english=sub.english,
        )
        srt = self.paths.subtitles / f"{ctx.id}.srt"
        srt.write_text(to_srt(chunks), encoding="utf-8")
        ctx.state["subtitles"] = {"srt": str(srt), "words": str(words_file), "chunks": [c.model_dump() for c in chunks]}
        logger.info("자막 %d개 생성 → %s", len(chunks), srt.name)

    def step_render(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        library = AudioLibrary(self.paths.music, self.paths.sfx)
        bgm = library.pick_bgm(self._mood(ctx), seed=ctx.id) if self.settings.audio.bgm_enabled else None
        if self.settings.audio.bgm_enabled and not bgm:
            logger.warning("BGM 없음 (assets/music + music.json 확인, `python -m app.main setup` 으로 기본 BGM 생성)")
        renderer = VideoRenderer(self.settings, library)
        chunks = [SubtitleChunk(**c) for c in ctx.state["subtitles"]["chunks"]]
        out = self.paths.videos / f"{ctx.id}.mp4"

        def _render():
            return renderer.render(
                content.scenes, self._assets(ctx), self._timings(ctx), Path(ctx.state["narration"]["path"]),
                ctx.state["narration"]["duration"], chunks, bgm, content.title, out, self.work_dir(ctx),
                thumbnail_path=self.paths.thumbnails / f"{ctx.id}.jpg", hook_seed=ctx.id,
            )

        result = retry_call(_render, step="FFmpeg Render", max_attempts=2, delays=self.settings.retry.delays)
        ctx.state["render"] = {
            "video_path": str(result.video_path), "duration": result.duration,
            "thumbnail": str(result.thumbnail_path) if result.thumbnail_path else None,
            "bgm": bgm.model_dump() if bgm else None, "sfx": result.sfx_used,
            "hook_effect": result.hook_effect,
        }
        self.jobs.update(ctx.id, video_path=str(result.video_path))

    def step_quality(self, ctx: JobContext) -> None:
        report = QualityChecker(self.settings.quality).check(
            Path(ctx.state["render"]["video_path"]), Path(ctx.state["subtitles"]["srt"])
        )
        ctx.state["quality"] = report.to_dict()
        if not report.passed:
            raise QualityCheckFailed("품질 검사 실패 → 업로드 금지: " + ", ".join(f"{c.name}({c.detail})" for c in report.failures()))

    def step_metadata(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        research = self._research(ctx)
        assets = self._assets(ctx)
        bgm = self._bgm(ctx)
        if self.options.offline:
            fx = self._fixture()["content"]
            meta = finalize_metadata(
                VideoMetadata(title=fx.get("title", content.title), description=fx.get("description", ""),
                              hashtags=fx.get("hashtags", []), tags=fx.get("tags", []),
                              title_en=fx.get("title_en", ""), description_en=fx.get("description_en", "")),
                content, research, assets, bgm,
            )
        else:
            previous_titles = [p["title"] for p in self._previous(ctx) if p["title"]]
            meta = MetadataGenerator(self.gemini, self.settings).generate(content, research, assets, bgm, previous_titles)
        ctx.state["metadata"] = meta.model_dump()
        content.description, content.hashtags = meta.description, meta.hashtags
        ctx.state["content"] = content.model_dump()

    def step_review(self, ctx: JobContext) -> None:
        cfg = self.settings.review
        previous_state = ctx.state.get("review") or {}
        approved = bool(previous_state.get("approved_by_user"))
        if not cfg.enabled:
            ctx.state["review"] = {"status": "skipped", "passed": True, "issues": []}
            return
        content = self._content(ctx)
        meta = VideoMetadata(**ctx.state["metadata"])
        previous = self._previous(ctx)
        previous_titles = [p["title"] for p in previous if p["title"]]
        draft = ctx.state.get("draft") or {}

        def _review():
            return review_content(self.settings, content, meta, self._research(ctx), self._assets(ctx), self._bgm(ctx),
                                  previous, hook_type=draft.get("hook_type", ""), offline=self.options.offline)

        report = _review()
        fixes: list[str] = []
        for _ in range(cfg.max_fix_attempts):
            if not report.fixable:
                break
            new_title = fix_title(meta, report.issues, previous_titles, self.settings)
            if not new_title:
                break
            old = meta.title
            cand = next((c for c in meta.title_selection if c.get("title") == new_title), {})
            if cand.get("title_en") and meta.title_en:
                meta.description = meta.description.replace(meta.title_en, cand["title_en"], 1)
                meta.title_en = cand["title_en"]
            meta.title, meta.title_strategy = new_title, cand.get("strategy", meta.title_strategy)
            fixes.append(f"제목 변경: {old} → {new_title}")
            report = _review()
        report.fixes = fixes
        report.approved_by_user = approved
        ctx.state["metadata"] = meta.model_dump()
        ctx.state["review"] = report.to_dict()
        for issue in report.issues:
            log = logger.error if issue.severity == "critical" else logger.warning if issue.severity == "minor" else logger.info
            log("콘텐츠 검수 [%s] %s: %s", issue.severity, issue.code, issue.message)
        logger.info("콘텐츠 검수: %s (독창성 보조 지표 %.2f, 최근 대본 최대 유사도 %.2f)", report.status,
                    report.originality_score, report.script_similarity)
        if report.critical:
            if approved:
                logger.warning("critical 문제가 있지만 사용자가 승인함 (upload --approve-review) → 진행")
                ctx.state["review"]["passed"] = True
                return
            self._save(ctx, "review")
            raise ContentReviewFailed(
                "콘텐츠 검수 실패 → 업로드 금지 (검토 필요): " + "; ".join(i.message for i in report.critical)
                + f" | 확인 후 진행하려면: python -m app.main upload {ctx.id} --approve-review"
            )

    def step_upload(self, ctx: JobContext) -> None:
        if not ctx.state.get("quality", {}).get("passed"):
            raise QualityCheckFailed("품질 검사를 통과하지 않은 영상은 업로드할 수 없습니다")
        if self.settings.review.enabled and not (ctx.state.get("review") or {}).get("passed"):
            raise ContentReviewFailed("콘텐츠 검수를 통과하지 않은 영상은 업로드할 수 없습니다")
        meta = VideoMetadata(**ctx.state["metadata"])
        plan = plan_publish(self.settings)
        result = YouTubeUploader(self.settings).upload(Path(ctx.state["render"]["video_path"]), meta, plan)
        ctx.state["upload"] = {
            "video_id": result.video_id, "url": result.url, "privacy_status": result.privacy_status,
            "publish_at": result.publish_at, "uploaded_at": now_iso(self.settings.app.timezone),
        }
        self.jobs.update(
            ctx.id, youtube_video_id=result.video_id, youtube_url=result.url, publish_at=result.publish_at,
            uploaded_at=ctx.state["upload"]["uploaded_at"],
        )
        if ctx.state.get("topic_id"):
            self.topics.update(ctx.state["topic_id"], video_id=result.video_id, youtube_url=result.url, status=TopicStatus.UPLOADED)

    def step_record(self, ctx: JobContext) -> None:
        content = self._content(ctx)
        upload = ctx.state.get("upload") or {}
        research = self._research(ctx)
        meta = ctx.state.get("metadata") or {}
        record = {
            "job_id": ctx.id,
            "video_id": upload.get("video_id", ""),
            "title": meta.get("title", content.title),
            "category": content.category,
            "topic": content.topic,
            "truth_status": content.truth_status,
            "hook": content.hook,
            "script": content.script,
            "scenes": [s.model_dump() for s in content.scenes],
            "description": meta.get("description", ""),
            "hashtags": meta.get("hashtags", []),
            "sources": [s.model_dump(exclude={"extract"}) for s in (research.sources if research else [])],
            "assets": ctx.state.get("assets", []),
            "bgm": ctx.state.get("render", {}).get("bgm"),
            "video_path": ctx.state.get("render", {}).get("video_path"),
            "duration": ctx.state.get("render", {}).get("duration"),
            "quality": ctx.state.get("quality"),
            "created_at": ctx.job.created_at,
            "uploaded_at": upload.get("uploaded_at", ""),
            "publish_at": upload.get("publish_at"),
            "youtube_url": upload.get("url", ""),
            "status": "success" if upload else "rendered",
        }
        record["production"] = self._record_production(ctx, content, upload, meta)
        record["topic_selection"] = ctx.state.get("topic_selection")
        record["review"] = ctx.state.get("review")
        write_json(self.paths.metadata / f"{ctx.id}.json", record)

        uploaded = bool(upload)
        final_status = JobStatus.SUCCESS if uploaded or self.options.offline else JobStatus.RENDERED
        self.jobs.update(ctx.id, status=final_status, error=None)
        if ctx.state.get("topic_id") and not uploaded:
            self.topics.update(ctx.state["topic_id"], status=TopicStatus.PRODUCED, title=record["title"])
        elif ctx.state.get("topic_id"):
            self.topics.update(ctx.state["topic_id"], title=record["title"])
        if self.settings.app.cleanup_work_files and final_status == JobStatus.SUCCESS:
            remove_dir(self.work_dir(ctx))
        logger.info("제작 이력 저장: %s (%s)", record["title"], record["youtube_url"] or record["video_path"])

    def _record_production(self, ctx: JobContext, content: ShortsContent, upload: dict, meta: dict) -> dict:
        """영상별 제작 조건 저장 (Analytics 성과와 연결해 비교). 실패해도 작업은 성공으로 둔다."""
        fields = _production_fields(self.settings, ctx.state, content, upload, meta)
        try:
            self.prod_repo.upsert(ctx.id, **fields)
        except Exception as exc:
            logger.warning("제작 조건 기록 실패: %s", exc)
        return fields


def _production_fields(settings: Settings, state: dict, content: ShortsContent, upload: dict, meta: dict) -> dict:
    draft = state.get("draft") or {}
    sel = state.get("topic_selection") or {}
    render = state.get("render") or {}
    hook = render.get("hook_effect") or {}
    review = state.get("review") or {}
    sources = Counter(a.get("source", "") for a in state.get("assets", []))
    chunks = (state.get("subtitles") or {}).get("chunks") or []
    return {
        "youtube_video_id": upload.get("video_id") or None,
        "topic_category": content.category,
        "topic_score": sel.get("score"),
        "trend_sources": sel.get("trend_sources") or [],
        "hook_type": draft.get("hook_type") or "other",
        "story_structure": " → ".join(draft.get("story_beats") or []),
        "loop_ending_enabled": bool(draft.get("loop")),
        "loop_quality_score": draft.get("loop_quality"),
        "visual_style": sources.most_common(1)[0][0] if sources else "unknown",
        "hook_effect_preset": hook.get("preset") if hook.get("applied") else "none",
        "video_duration": render.get("duration"),
        "subtitle_mode": "ko+en" if any(c.get("text_en") for c in chunks) else "ko",
        "title_strategy": meta.get("title_strategy") or "default",
        "script_similarity_score": draft.get("script_similarity"),
        "originality_score": review.get("originality_score"),
        "extra_json": {
            "visual_sources": dict(sources), "ai_style": settings.assets.ai_image.style,
            "loop_note": draft.get("loop_note"), "hook_effect": hook, "topic_strategy": sel.get("strategy"),
            "review_issues": [i.get("code") for i in review.get("issues", [])],
        },
    }


def today_start_iso(settings: Settings) -> str:
    tz = ZoneInfo(settings.app.timezone)
    return datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
