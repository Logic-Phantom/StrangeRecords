"""CLI 진입점.

python -m app.main today     오늘 영상 제작 (production 모드면 YouTube 업로드 + 12:00 예약 공개)
python -m app.main test      영상까지 제작 (업로드 안 함)  [--offline: Gemini 없이 샘플 대본으로]
python -m app.main topic     주제 후보 생성 + 중복 검사 미리보기 (DB 저장 안 함)
python -m app.main history   제작 이력
python -m app.main retry     실패 작업을 실패한 단계부터 재시도
python -m app.main upload    제작 완료(업로드 대기) 영상 업로드
python -m app.main auth      YouTube OAuth 최초 인증
python -m app.main setup     폴더/DB/기본 BGM·효과음 생성
python -m app.main doctor    환경 점검 (Python, FFmpeg, API Key, 폰트, 토큰)
python -m app.main models    사용 가능한 Gemini 모델 목록
python -m app.main schedule  보조 Python 스케줄러 (매일 generate_time 에 today 실행)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config.settings import Settings, get_settings
from app.utils.logger import setup_logging


def _pipeline(settings: Settings):
    from app.pipeline.daily_pipeline import DailyPipeline

    return DailyPipeline(settings)


# ------------------------------------------------------------------ today
def cmd_today(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.models import JobStatus
    from app.pipeline.daily_pipeline import RunOptions, today_start_iso
    from app.utils.logger import get_logger

    log = get_logger("cli")
    pipeline = _pipeline(settings)
    upload = settings.is_production and not args.no_upload
    since = today_start_iso(settings)

    if not args.force:
        done = pipeline.jobs.count_successful_since(since, upload_only=True) if upload else 0
        if done >= settings.schedule.daily_count:
            log.info("오늘 이미 %d개 업로드 완료 (daily_count=%d) → 종료. 강제 실행은 --force", done, settings.schedule.daily_count)
            return 0
        # 오늘 실패한 작업이 있으면 새 주제 대신 이어서 실행
        latest = pipeline.jobs.latest()
        if latest and latest.created_at >= since and latest.status in (JobStatus.FAILED, JobStatus.RUNNING) and latest.retry_count < 3:
            log.info("오늘 실패한 작업 %s 이어서 실행", latest.id)
            pipeline.resume(latest.id, upload=upload)
            return 0
        if latest and latest.created_at >= since and latest.status == JobStatus.RENDERED and upload:
            log.info("오늘 제작 완료된 작업 %s 업로드", latest.id)
            pipeline.resume(latest.id, upload=True)
            return 0

    pipeline.run_new(RunOptions(
        upload=upload, forced_topic=args.topic, forced_category=args.category,
        mode_label=settings.app.mode,
    ))
    return 0


def cmd_test(settings: Settings, args: argparse.Namespace) -> int:
    from app.pipeline.daily_pipeline import RunOptions

    if not args.offline and not settings.secrets.gemini_api_key:
        print("GEMINI_API_KEY 가 없습니다. .env 를 설정하거나 `python -m app.main test --offline` 으로 샘플 대본 테스트를 실행하세요.")
        return 2
    ctx = _pipeline(settings).run_new(RunOptions(
        upload=False, offline=args.offline, fixture=Path(args.fixture) if args.fixture else None,
        forced_topic=args.topic, forced_category=args.category,
        mode_label="test-offline" if args.offline else "test",
    ))
    print(f"\n✅ 테스트 영상: {ctx.state['render']['video_path']}")
    return 0


def cmd_topic(settings: Settings, args: argparse.Namespace) -> int:
    from app.ai.gemini_client import GeminiClient
    from app.ai.topic_generator import TopicGenerator
    from app.database.db import Database
    from app.database.repositories import TopicRepository
    from app.topics.duplicate_checker import DuplicateChecker
    from app.topics.selector import TopicSelector

    settings.paths.ensure()
    client = GeminiClient(settings.gemini, settings.secrets.gemini_api_key, settings.retry)
    topics = TopicRepository(Database(settings.paths.database), settings.app.timezone)
    selector = TopicSelector(settings, TopicGenerator(client, settings), DuplicateChecker(settings.duplicate, client), topics)
    result = selector.select(args.category)
    print(f"\n카테고리: {result.category.name}")
    for r in result.checked:
        mark = "❌ 중복" if r.duplicate else "✅"
        extra = f" ≈ {r.similar_to} ({r.method})" if r.duplicate else ""
        print(f"  {mark} {r.candidate.title} — {r.candidate.topic}{extra}")
    print(f"\n👉 선택: {result.selected.title} [{result.selected.truth_status}]\n   {result.selected.summary}")
    print("(미리보기 전용 - DB 에 저장하지 않음)")
    return 0


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    pipeline = _pipeline(settings)
    jobs = pipeline.jobs.list(args.limit)
    if not jobs:
        print("제작 이력이 없습니다.")
        return 0
    print(f"{'JOB':<16} {'STATUS':<9} {'STEP':<9} {'CATEGORY':<19} TITLE / URL")
    print("-" * 100)
    for job in jobs:
        topic = pipeline.topics.get(job.topic_id) if job.topic_id else None
        title = (job.state.get("metadata") or {}).get("title") or (topic.title if topic else job.state.get("topic", {}).get("title", "-"))
        category = topic.category if topic else job.state.get("topic", {}).get("category", "-")
        step = job.current_step or ("done" if job.status != "failed" else "-")
        print(f"{job.id:<16} {job.status:<9} {step:<9} {category:<19} {title}")
        if job.youtube_url:
            print(f"{'':<56}{job.youtube_url}  (publishAt {job.publish_at or '-'})")
        if job.error:
            print(f"{'':<56}⚠ {job.error[:120]}")
    return 0


def cmd_retry(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.models import JobStatus

    pipeline = _pipeline(settings)
    job = pipeline.jobs.get(args.job_id) if args.job_id else pipeline.jobs.latest([JobStatus.FAILED])
    if not job:
        print("재시도할 실패 작업이 없습니다.")
        return 0
    pipeline.resume(job.id)
    return 0


def cmd_upload(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.models import JobStatus

    pipeline = _pipeline(settings)
    job = pipeline.jobs.get(args.job_id) if args.job_id else pipeline.jobs.latest([JobStatus.RENDERED])
    if not job:
        print("업로드 대기 중인 영상이 없습니다 (status=rendered).")
        return 0
    if job.state.get("options", {}).get("offline"):
        print("offline 샘플 테스트 영상은 업로드하지 않습니다.")
        return 2
    pipeline.resume(job.id, upload=True)
    return 0


def cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    from app.youtube.auth import build_service

    service = build_service(settings, interactive=True)
    channels = service.channels().list(part="snippet", mine=True).execute()
    for item in channels.get("items", []):
        print(f"✅ 인증된 채널: {item['snippet']['title']} ({item['id']})")
    return 0


def cmd_setup(settings: Settings, args: argparse.Namespace) -> int:
    from app.assets.synth_audio import generate_default_audio
    from app.database.db import Database

    settings.paths.ensure()
    Database(settings.paths.database)
    generate_default_audio(settings.paths.music, settings.paths.sfx, overwrite=args.overwrite)
    env = settings.paths.root / ".env"
    if not env.exists():
        env.write_text((settings.paths.root / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
        print("📝 .env 생성 → API Key 를 입력하세요")
    print("✅ setup 완료")
    return 0


def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    import platform

    from app.video.ffmpeg import ffmpeg_bin, ffmpeg_version, ffprobe_bin
    from app.video.subtitle_renderer import find_font
    from app.youtube.auth import token_path

    ok = True

    def line(name: str, passed: bool, detail: str, required: bool = True) -> None:
        nonlocal ok
        mark = "✅" if passed else ("❌" if required else "⚠️ ")
        if required and not passed:
            ok = False
        print(f"{mark} {name:<22} {detail}")

    line("OS", True, f"{platform.system()} {platform.release()}")
    line("Python", sys.version_info >= (3, 11), sys.version.split()[0])
    try:
        line("FFmpeg", True, f"{ffmpeg_version()} ({ffmpeg_bin()})")
        line("ffprobe", bool(ffprobe_bin()), ffprobe_bin() or "없음 → PyAV 로 분석 (정상 동작)", required=False)
    except Exception as exc:
        line("FFmpeg", False, str(exc))
    for module in ("google.genai", "edge_tts", "faster_whisper", "PIL", "googleapiclient", "av"):
        try:
            __import__(module)
            line(f"pkg {module}", True, "OK")
        except ImportError as exc:
            line(f"pkg {module}", False, str(exc))
    s = settings.secrets
    line("GEMINI_API_KEY", bool(s.gemini_api_key), "설정됨" if s.gemini_api_key else "없음 (필수)")
    line("PEXELS_API_KEY", bool(s.pexels_api_key), "설정됨" if s.pexels_api_key else "없음 → Pixabay/로컬/자체 그래픽 사용", required=False)
    line("PIXABAY_API_KEY", bool(s.pixabay_api_key), "설정됨" if s.pixabay_api_key else "없음", required=False)
    yt_client = bool(s.youtube_client_id and s.youtube_client_secret) or (settings.paths.root / settings.youtube.client_secrets_file).exists()
    line("YouTube client", yt_client, "설정됨" if yt_client else "없음 (production 업로드에 필요)", required=settings.is_production)
    line("YouTube token", token_path(settings).exists(), "있음" if token_path(settings).exists() else "없음 → python -m app.main auth", required=settings.is_production)
    try:
        line("한글 폰트", True, find_font(settings.subtitle, settings.paths.fonts))
    except FileNotFoundError as exc:
        line("한글 폰트", False, str(exc))
    from app.assets.audio_library import AudioLibrary

    lib = AudioLibrary(settings.paths.music, settings.paths.sfx)
    line("BGM", bool(lib.tracks()), f"{len(lib.tracks())}곡" if lib.tracks() else "없음 → python -m app.main setup", required=False)
    line("Gemini model", True, settings.gemini.model)
    line("Mode", True, settings.app.mode)
    return 0 if ok else 1


def cmd_models(settings: Settings, args: argparse.Namespace) -> int:
    from app.ai.gemini_client import GeminiClient

    client = GeminiClient(settings.gemini, settings.secrets.gemini_api_key, settings.retry)
    print(f"현재 설정 모델: {settings.gemini.model}\n사용 가능한 모델:")
    for name in client.list_models():
        print(f"  {'*' if name == settings.gemini.model else ' '} {name}")
    print("\n무료 티어 한도: https://ai.google.dev/gemini-api/docs/rate-limits")
    return 0


def cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    from app.scheduler.scheduler import run_forever

    ns = argparse.Namespace(force=False, no_upload=False, topic=None, category=None)
    run_forever(settings, lambda: cmd_today(settings, ns))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.main", description="기묘한 기록 - AI YouTube Shorts 자동 제작")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("today", help="오늘 영상 제작 (+ production 모드면 업로드)")
    p.add_argument("--force", action="store_true", help="오늘 이미 업로드했어도 새로 제작")
    p.add_argument("--no-upload", action="store_true", help="업로드하지 않음")
    p.add_argument("--topic", help="주제 직접 지정")
    p.add_argument("--category", help="카테고리 id 지정")
    p.set_defaults(func=cmd_today)

    p = sub.add_parser("test", help="영상까지 제작 (업로드 안 함)")
    p.add_argument("--offline", action="store_true", help="Gemini 없이 샘플 대본(fixture)으로 테스트")
    p.add_argument("--fixture", help="offline 테스트용 JSON 경로")
    p.add_argument("--topic", help="주제 직접 지정")
    p.add_argument("--category", help="카테고리 id 지정")
    p.set_defaults(func=cmd_test)

    p = sub.add_parser("topic", help="주제 후보 생성 + 중복 검사 미리보기")
    p.add_argument("--category", help="카테고리 id 지정")
    p.set_defaults(func=cmd_topic)

    p = sub.add_parser("history", help="제작 이력")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("retry", help="실패 작업 재시도")
    p.add_argument("job_id", nargs="?")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("upload", help="제작 완료 영상 업로드")
    p.add_argument("job_id", nargs="?")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("auth", help="YouTube OAuth 인증")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("setup", help="폴더/DB/기본 오디오 생성")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("doctor", help="환경 점검")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("models", help="사용 가능한 Gemini 모델 목록")
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("schedule", help="보조 Python 스케줄러")
    p.set_defaults(func=cmd_schedule)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    setup_logging(settings.paths.logs, settings.logging.level)
    try:
        return args.func(settings, args)
    except KeyboardInterrupt:
        print("\n중단됨")
        return 130
    except Exception as exc:
        from app.utils.logger import get_logger

        get_logger("cli").error("실패: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
