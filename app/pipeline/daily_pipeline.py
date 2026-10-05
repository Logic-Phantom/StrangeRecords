"""Daily Pipeline

[1] DB 기존 주제 조회 → [2] Gemini 주제 후보 → [3] 중복 검사 → [4] 최종 주제
→ [5] 자료 조사 → [6] Gemini 대본 → [7] 대본 검증 → [8] Scene 생성
→ [9] Edge TTS 음성 (Scene 별 실제 길이 확정) → [10] Pexels/Pixabay 자료 수집
→ [11] Whisper 자막 → [12] FFmpeg 영상 → [13] 품질 검사
→ [14] Gemini 제목/설명/해시태그 → [15] YouTube 업로드 + [16] 12:00 예약 공개 → [17] SQLite 기록

각 단계 결과는 DB(jobs.state_json)와 data/, output/ 에 저장되므로
실패한 작업은 `python -m app.main retry` 로 실패한 단계부터 다시 실행된다.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.ai.gemini_client import GeminiClient
from app.ai.metadata_generator import MetadataGenerator, finalize_metadata
from app.ai.scene_generator import SceneGenerator, renumber
from app.ai.script_generator import ScriptGenerator
from app.ai.topic_generator import TopicGenerator
from app.assets.asset_manager import AssetManager, build_providers
from app.assets.audio_library import AudioLibrary
from app.config.settings import Settings
from app.database.db import Database
from app.database.models import JobRecord, JobStatus, TopicStatus
from app.database.repositories import AssetRepository, JobRepository, StepLogRepository, TopicRepository
from app.quality.checker import QualityChecker
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

STEPS = ["topic", "research", "script", "scenes", "voice", "assets", "subtitles", "render", "quality", "metadata", "upload", "record"]


class QualityCheckFailed(NonRetryableError):
    pass


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
        self._gemini: GeminiClient | None = None
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
        self.jobs.update(ctx.id, status=JobStatus.FAILED, error=f"[{step}] {type(exc).__name__}: {exc}"[:2000])
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
            selector = TopicSelector(
                self.settings,
                TopicGenerator(self.gemini, self.settings),
                DuplicateChecker(self.settings.duplicate, self.gemini),
                self.topics,
            )
            result = selector.select(self.options.forced_category)
            candidate = result.selected
            selection = result.to_dict()

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
            draft = ScriptGenerator(self.gemini, self.settings).generate(self._candidate(ctx), self._research(ctx))
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
        manager = AssetManager(
            self.settings,
            build_providers(self.settings, self.gemini if self.settings.assets.ai_image.enabled else None),
            used_ids=used,
        )
        assets = manager.collect(content.scenes, self.work_dir(ctx) / "assets", self._mood(ctx), self._timings(ctx))
        ctx.state["assets"] = [a.model_dump() for a in assets]
        self.assets_repo.replace_for_job(ctx.id, assets)
        write_json(self.paths.sources / f"{ctx.id}_assets.json", ctx.state["assets"])

    def step_subtitles(self, ctx: JobContext) -> None:
        content = self._content(ctx)
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
            text_source=sub.text_source, max_chars=sub.max_chars, max_words=sub.max_words,
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
                thumbnail_path=self.paths.thumbnails / f"{ctx.id}.jpg",
            )

        result = retry_call(_render, step="FFmpeg Render", max_attempts=2, delays=self.settings.retry.delays)
        ctx.state["render"] = {
            "video_path": str(result.video_path), "duration": result.duration,
            "thumbnail": str(result.thumbnail_path) if result.thumbnail_path else None,
            "bgm": bgm.model_dump() if bgm else None, "sfx": result.sfx_used,
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
                              hashtags=fx.get("hashtags", []), tags=fx.get("tags", [])),
                content, research, assets, bgm,
            )
        else:
            meta = MetadataGenerator(self.gemini, self.settings).generate(content, research, assets, bgm)
        ctx.state["metadata"] = meta.model_dump()
        content.description, content.hashtags = meta.description, meta.hashtags
        ctx.state["content"] = content.model_dump()

    def step_upload(self, ctx: JobContext) -> None:
        if not ctx.state.get("quality", {}).get("passed"):
            raise QualityCheckFailed("품질 검사를 통과하지 않은 영상은 업로드할 수 없습니다")
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


def today_start_iso(settings: Settings) -> str:
    tz = ZoneInfo(settings.app.timezone)
    return datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
