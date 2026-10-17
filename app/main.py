"""CLI 진입점.

python -m app.main today     오늘 영상 제작 (production 모드면 YouTube 업로드 + 12:00 예약 공개)
python -m app.main test      영상까지 제작 (업로드 안 함)  [--offline: Gemini 없이 샘플 대본으로]
python -m app.main topic     주제 후보 생성 + 중복 검사 미리보기 (DB 저장 안 함)
python -m app.main history   제작 이력
python -m app.main retry     실패 작업을 실패한 단계부터 재시도
python -m app.main remake    완료된 작업을 지정 단계부터 다시 제작 + 업로드 (--from-step voice)
python -m app.main upload    제작 완료(업로드 대기) 영상 업로드 (--approve-review: 검수 보류 작업을 확인 후 진행)
python -m app.main analytics sync|report|status|import-csv   YouTube 성과 수집 / 보고서 / 권한 점검 / Studio CSV
python -m app.main trends    외부 트렌드 신호 미리보기 (저장만, 주제 선정 안 함)
python -m app.main comments status|retry|preview|pins|mark-pinned|guide   자동 댓글 / 수동 고정 기록
python -m app.main review    콘텐츠 검수 결과 조회 (사실성·출처·장면·라이선스·광고 적합성)
python -m app.main rights    음성(Edge TTS) 상업적 이용 권리 상태
python -m app.main branding status|check|assets|apply   채널 브랜딩 (apply 는 기본 미리보기, --yes 로만 적용)
python -m app.main auth      YouTube OAuth 인증 (업로드 + 읽기 + Analytics 읽기 + 댓글 권한)
python -m app.main setup     폴더/DB/기본 BGM·효과음 생성
python -m app.main doctor    환경 점검 (Python, FFmpeg, API Key, 폰트, 토큰)
python -m app.main models    사용 가능한 Gemini 모델 목록
python -m app.main schedule  보조 Python 스케줄러 (매일 generate_time 에 today 실행)
"""

from __future__ import annotations

import argparse
import json
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
    from app.analytics.insights import PerformanceInsights
    from app.database.db import Database
    from app.database.repositories import TopicRepository, TrendRepository
    from app.topics.duplicate_checker import DuplicateChecker
    from app.topics.selector import TopicSelector
    from app.trends.collector import TrendCollector

    settings.paths.ensure()
    db = Database(settings.paths.database)
    client = GeminiClient(settings.gemini, settings.secrets.gemini_api_key, settings.retry)
    topics = TopicRepository(db, settings.app.timezone)
    trends = TrendCollector(settings, TrendRepository(db)) if settings.trends.enabled and not args.no_trends else None
    insights = PerformanceInsights(db, settings) if settings.analytics.enabled else None
    selector = TopicSelector(settings, TopicGenerator(client, settings), DuplicateChecker(settings.duplicate, client), topics,
                             trends=trends, insights=insights)
    result = selector.select(args.category)
    print(f"\n카테고리: {result.category.name} ({result.category_reason})")
    if result.hints:
        print("트렌드 힌트:")
        for h in result.hints:
            print(f"  {h.line()}")
    for name, st in (result.trend_status.get("providers") or {}).items():
        print(f"  [{'on ' if st.get('available') else 'off'}] {name}: {st.get('detail')}")
    print(f"\n선정 방식: {result.strategy}")
    for s in sorted(result.scores, key=lambda x: x.final, reverse=True):
        r = s.result
        if r.duplicate:
            print(f"  ❌ 중복 {r.candidate.title} ≈ {r.similar_to} ({r.method})")
            continue
        comps = " ".join(f"{k}={v:.2f}" for k, v in s.components.items() if v is not None)
        print(f"  {'👉' if s.selected else '✅'} {s.final:.3f} {r.candidate.title} — {r.candidate.topic}\n       {comps}")
    print(f"\n👉 선택: {result.selected.title} [{result.selected.truth_status}]\n   {result.selected.summary}")
    if result.selected_score:
        for reason in result.selected_score.reasons:
            print(f"   - {reason}")
    print("(미리보기 전용 - 주제는 DB 에 저장하지 않음, 수집한 트렌드 신호만 캐시로 저장)")
    return 0


def cmd_trends(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.db import Database
    from app.database.repositories import TrendRepository
    from app.trends.collector import TrendCollector

    settings.paths.ensure()
    category = settings.category(args.category) if args.category else settings.categories[0]
    if not category:
        print(f"알 수 없는 카테고리: {args.category}")
        return 2
    collector = TrendCollector(settings, TrendRepository(Database(settings.paths.database)))
    print("Provider 상태:")
    for name, st in collector.statuses.items():
        print(f"  [{'on ' if st['available'] else 'off'}] {name}: {st['detail']}")
    hints = collector.discover_hints(category)
    print(f"\n'{category.name}' 관련 트렌드 힌트 {len(hints)}개:")
    for h in hints:
        print(f"  {h.line()}  {h.source_url}")
    if args.keyword:
        from app.schemas import TopicCandidate

        sigs = collector.candidate_signals([TopicCandidate(title=args.keyword, topic=args.keyword, category=category.id,
                                                           search_terms_ko=[args.keyword])])[0]
        print(f"\n'{args.keyword}' 신호:")
        for s in sigs:
            print(f"  {s.provider}/{s.kind} [{s.status}] 관심도={s.interest} (정규화 {s.interest_normalized}) 증가={s.velocity}"
                  f" 영상수={s.video_count} 과밀도={s.saturation} 기간={str(s.period_start)[:10]}~{str(s.period_end)[:10]} {s.source_url}")
    for e in collector.errors:
        print(f"  ⚠ {e}")
    return 0


def cmd_analytics(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.db import Database

    settings.paths.ensure()
    db = Database(settings.paths.database)
    action = args.analytics_command
    if action == "status":
        from app.analytics.youtube_analytics import ANALYTICS_SCOPE, REAUTH_GUIDE, token_scopes

        scopes = token_scopes(settings)
        print(f"token.json 범위: {', '.join(s.rsplit('/', 1)[-1] for s in scopes) or '(없음)'}")
        if ANALYTICS_SCOPE in scopes:
            print("✅ Analytics 읽기 권한 있음 (YouTube Analytics API 사용 설정은 실제 sync 로 확인)")
        else:
            print("⚠️  " + REAUTH_GUIDE)
        print(f"DB 스키마 버전: {db.schema_version()}")
        return 0
    if action == "sync":
        from app.analytics.sync import AnalyticsSync

        summary = AnalyticsSync(settings, db).run(days=args.days, video_id=args.video)
        print(summary.line())
        for m in summary.messages:
            print(f"  - {m}")
        return 0
    if action == "report":
        from app.analytics.report import build_report, export_report, format_report

        report = build_report(db, settings, days=args.days)
        print(format_report(report))
        if args.format != "text":
            out = Path(args.output) if args.output else settings.paths.reports / f"performance.{args.format}"
            export_report(report, out, args.format)
            print(f"\n💾 {args.format.upper()} 저장: {out}")
        return 0
    if action == "import-csv":
        from app.analytics.studio_import import import_studio_csv
        from app.database.repositories import PerformanceRepository

        result = import_studio_csv(Path(args.file), PerformanceRepository(db))
        print(f"Studio CSV 가져오기: {result.rows}행 → 지표 {result.stored}개 저장 ({', '.join(result.metrics)})")
        if result.skipped:
            print(f"  건너뜀(영상 ID 아님): {len(result.skipped)}행")
        if result.ignored_columns:
            print(f"  무시한 열: {', '.join(result.ignored_columns[:10])}")
        return 0
    return 2


def cmd_comments(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.db import Database
    from app.database.repositories import CommentRepository, JobRepository
    from app.youtube.comments import COMMENT_SCOPE, REAUTH_GUIDE, CommentService, PinStatus, granted_scopes, write_pin_guide

    settings.paths.ensure()
    db = Database(settings.paths.database)
    repo = CommentRepository(db, settings.app.timezone)
    action = args.comments_command

    def _gemini():
        if args.__dict__.get("offline") or not settings.comments.use_gemini or not settings.secrets.gemini_api_key:
            return None
        from app.ai.gemini_client import GeminiClient

        return GeminiClient(settings.gemini, settings.secrets.gemini_api_key, settings.retry)

    if action == "status":
        has_scope = COMMENT_SCOPE in granted_scopes(settings)
        print(f"자동 댓글: {'켜짐' if settings.comments.enabled else '꺼짐'} (comments.enabled) / "
              f"댓글 권한: {'있음' if has_scope else '없음 (youtube.force-ssl)'} / 고정: API 미지원 → 수동")
        if not has_scope:
            print("⚠️  " + REAUTH_GUIDE)
        rows = repo.list(limit=args.limit)
        if not rows:
            print("댓글 기록이 없습니다.")
            return 0
        service = CommentService(settings, db) if args.verify else None
        print(f"{'VIDEO':<13} {'STATUS':<18} {'PIN':<16} {'TRY':<3} {'JOB':<16} 본문 / 오류")
        for r in rows:
            status = r["status"]
            if service:
                try:
                    status = service.verify(r)
                except Exception as exc:
                    status = f"{status}(확인 실패)"
                    print(f"  ⚠ {r['youtube_video_id']} 확인 실패: {str(exc).splitlines()[0][:120]}")
            print(f"{r['youtube_video_id']:<13} {status:<18} {r['pin_status']:<16} {r['attempts']:<3} {r.get('job_id') or '-':<16} "
                  f"{r['comment_text'][:60]}")
            if r.get("error_code"):
                print(f"{'':<13} ⚠ {r['error_code']}: {(r.get('error_message') or '')[:100]}")
        return 0
    if action == "preview":
        job = JobRepository(db, settings.app.timezone).get(args.job_id) if args.job_id else \
            JobRepository(db, settings.app.timezone).latest()
        if not job:
            print("작업이 없습니다.")
            return 1
        draft = CommentService(settings, db, gemini=_gemini()).generate_text(job.state, exclude_video=job.youtube_video_id)
        print(f"[{draft.generator} / {draft.comment_type}] {draft.text}")
        for p in draft.problems:
            print(f"  ❌ {p}")
        print("(미리보기 전용 - 저장/등록하지 않음)" if draft.ok else "→ 검수 실패: 이 본문은 등록되지 않습니다")
        return 0 if draft.ok else 1
    if action == "retry":
        if not settings.comments.enabled and not args.force:
            print("comments.enabled=false → 등록하지 않습니다 (설정을 켜거나 --force).")
            return 0
        service = CommentService(settings, db, gemini=_gemini())
        results = service.retry_all(job_id=args.job_id, regenerate=args.regenerate) if settings.comments.enabled else []
        if args.force and not settings.comments.enabled and args.job_id:
            job = service.jobs.get(args.job_id)
            results = [(args.job_id, service.process_job(job, regenerate=args.regenerate, force=True))] if job else []
        if not results:
            print("재시도할 댓글이 없습니다.")
        for job_id, res in results:
            print(f"  {job_id}: {res.get('status')} {res.get('comment_id') or res.get('error_code') or res.get('message') or ''}")
        return 0
    if action == "pins":
        rows = repo.list(statuses=["posted"], pin_statuses=[PinStatus.MANUAL_REQUIRED, PinStatus.UNKNOWN], limit=args.limit)
        print("고정 처리가 필요한 댓글 (YouTube API 에 고정 기능 없음 → Studio 에서 직접 고정):" if rows else "고정 대기 댓글이 없습니다.")
        for r in rows:
            print(f"  https://youtube.com/shorts/{r['youtube_video_id']}  comment={r.get('comment_id')}  [{r['pin_status']}]")
            print(f"    {r['comment_text']}")
        return 0
    if action == "mark-pinned":
        ok, message = CommentService(settings, db).mark_pinned(args.target, note=args.note or "", undo=args.undo)
        print(("✅ " if ok else "❌ ") + message)
        return 0 if ok else 1
    if action == "guide":
        out = Path(args.output) if args.output else settings.paths.reports / "pin_guide.md"
        print(f"📝 고정 댓글 운영 가이드: {write_pin_guide(settings, db, out)}")
        return 0
    return 2


def cmd_review(settings: Settings, args: argparse.Namespace) -> int:
    """콘텐츠 검수 결과 조회 (사실성·출처·장면·라이선스·AI 고지·광고 적합성)."""
    from app.database.db import Database
    from app.database.repositories import JobRepository

    jobs = JobRepository(Database(settings.paths.database), settings.app.timezone)
    job = jobs.get(args.job_id) if args.job_id else next((j for j in jobs.list(30) if j.state.get("review")), None)
    if not job or not job.state.get("review"):
        print("검수 결과가 있는 작업이 없습니다.")
        return 1
    review = job.state["review"]
    print(f"작업 {job.id} ({job.status}) 검수: {review.get('status')}"
          f"{' / 사람이 보류 항목 확인(--approve-review)' if review.get('approved_by_user') else ''}")
    ad = review.get("ad_suitability") or {}
    if ad:
        print(f"광고 적합성(보조 지표, 수익화 보장 아님): {ad.get('status')}  {', '.join(ad.get('reasons') or []) or ''}")
    print(f"독창성 보조 지표 {review.get('originality_score')} / 최근 대본 최대 유사도 {review.get('script_similarity')}")
    for issue in review.get("issues", []):
        mark = {"critical": "❌", "minor": "⚠️ ", "warning": "·"}.get(issue.get("severity"), "-")
        print(f"  {mark} [{issue.get('severity')}] {issue.get('code')}: {issue.get('message')}")
    if review.get("approved_by_user") and any(i.get("severity") == "critical" for i in review.get("issues", [])):
        print("※ 승인은 업로드 진행 기록일 뿐, 위 사실/라이선스 문제가 해결됐다는 뜻이 아닙니다.")
    return 0


def cmd_rights(settings: Settings, args: argparse.Namespace) -> int:
    from app.voice.rights import voice_rights

    r = voice_rights(settings)
    print(f"음성(Edge TTS) 상업적 이용 권리: {r.status}  (설정값 {settings.voice.rights_status})")
    for line in r.notes:
        print(f"  - {line}")
    print(f"검수 처리: {r.review_severity} (review.unverified_rights_policy={settings.review.unverified_rights_policy},"
          f" strict={settings.review.strict})")
    return 0


def cmd_branding(settings: Settings, args: argparse.Namespace) -> int:
    from app.branding import channel as branding

    action = args.branding_command
    if action == "assets":
        for path in branding.generate_assets(settings):
            print(f"🖼  {path}")
        print("(파일 생성만 했습니다. 채널 적용은 `branding apply` 또는 수동 업로드)")
        return 0
    if action == "check":
        report = branding.check_consistency(settings)
        for item in report:
            print(f"{ {'ok': '✅', 'warn': '⚠️ ', 'fail': '❌'}.get(item['level'], '-') } {item['name']}: {item['detail']}")
        return 1 if any(i["level"] == "fail" for i in report) else 0
    service = branding.ChannelBranding(settings)
    if action == "status":
        current = service.current()
        print(json.dumps(branding.summarize(current), ensure_ascii=False, indent=2))
        return 0
    if action == "apply":
        plan = service.plan()
        print(branding.format_plan(plan))
        if not args.yes:
            print("\n(미리보기 / --dry-run) 실제 채널은 바꾸지 않았습니다. 적용하려면: python -m app.main branding apply --yes"
                  + (" --banner" if args.banner else ""))
            return 0
        result = service.apply(plan, banner=args.banner)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    return 2


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


def cmd_remake(settings: Settings, args: argparse.Namespace) -> int:
    """완료(업로드)된 작업을 지정 단계부터 다시 만들어 업로드한다 (예: 영상을 삭제하고 이미지/자막을 고쳐 다시 올릴 때).

    주제/대본/Scene 등 from_step 이전 단계 결과는 그대로 쓰고, 이전 YouTube 업로드 기록은 지운다.
    """
    from app.database.models import JobStatus
    from app.pipeline.daily_pipeline import STEPS, today_start_iso
    from app.utils.logger import get_logger

    log = get_logger("cli")
    pipeline = _pipeline(settings)
    if args.job_id:
        job = pipeline.jobs.get(args.job_id)
    else:
        job = pipeline.jobs.latest([JobStatus.SUCCESS, JobStatus.RENDERED])
        if job and job.created_at < today_start_iso(settings):
            job = None
    if not job:
        print("다시 만들 작업이 없습니다 (job_id 를 지정하세요).")
        return 1
    if args.from_step not in STEPS:
        print(f"단계 이름 오류: {args.from_step} (가능: {', '.join(STEPS)})")
        return 2
    keep = STEPS[: STEPS.index(args.from_step)]
    completed = [s for s in job.completed_steps if s in keep]
    from app.pipeline.daily_pipeline import OPTIONAL_STEPS

    # 나중에 추가된 단계(motion)가 없는 이전 작업도 다시 만들 수 있다
    missing = [s for s in keep if s not in completed and s not in OPTIONAL_STEPS]
    if missing:
        print(f"{args.from_step} 이전 단계가 완료되지 않았습니다: {missing}")
        return 2
    state = dict(job.state)
    state.pop("upload", None)
    if STEPS.index(args.from_step) <= STEPS.index("assets"):
        # 이전 실행에서 그린 이미지 대신 새로 그린다
        (settings.paths.sources / f"{job.id}_assets_partial.json").unlink(missing_ok=True)
    pipeline.jobs.save_state(job.id, completed, state, None)
    pipeline.jobs.update(job.id, status=JobStatus.FAILED, youtube_video_id=None, youtube_url=None,
                         uploaded_at=None, publish_at=None, retry_count=0,
                         error=f"remake from {args.from_step} (이전 영상: {job.youtube_url or '-'})")
    log.info("작업 %s 을(를) %s 단계부터 다시 제작 (이전 영상 %s)", job.id, args.from_step, job.youtube_url or "-")
    ctx = pipeline.resume(job.id, upload=settings.is_production)
    up = ctx.state.get("upload") or {}
    print(f"\n✅ 다시 제작 완료: {up.get('url') or ctx.state.get('render', {}).get('video_path')}")
    return 0


def cmd_upload(settings: Settings, args: argparse.Namespace) -> int:
    from app.database.models import JobStatus

    pipeline = _pipeline(settings)
    default_status = JobStatus.REVIEW_NEEDED if args.approve_review else JobStatus.RENDERED
    job = pipeline.jobs.get(args.job_id) if args.job_id else pipeline.jobs.latest([default_status])
    if not job:
        print(f"업로드 대기 중인 영상이 없습니다 (status={default_status}).")
        return 0
    if job.state.get("options", {}).get("offline"):
        print("offline 샘플 테스트 영상은 업로드하지 않습니다.")
        return 2
    if args.approve_review:
        review = dict(job.state.get("review") or {})
        review["approved_by_user"] = True
        state = dict(job.state)
        state["review"] = review
        pipeline.jobs.save_state(job.id, job.completed_steps, state, None)
        print(f"검수 보류 항목을 확인했다고 기록: {job.id}")
    if args.privacy:
        # 이번 업로드에만 적용 (config.yaml 은 그대로)
        settings.youtube.publish_mode = "private" if args.privacy == "private" else args.privacy
        settings.youtube.privacy_status = "private"
    ctx = pipeline.resume(job.id, upload=True)
    up = ctx.state.get("upload") or {}
    print(f"\n✅ 업로드 완료: {up.get('url')} ({up.get('privacy_status')}, publishAt={up.get('publish_at') or '-'})")
    return 0


def cmd_auth(settings: Settings, args: argparse.Namespace) -> int:
    from app.analytics.youtube_analytics import token_scopes
    from app.youtube.auth import build_service, requested_scopes

    wanted = requested_scopes(analytics=not args.no_analytics, comments=not args.no_comments)
    have = set(token_scopes(settings))
    if have and not set(wanted) <= have:
        # 기존 토큰은 덮어쓰기 전에 백업한다 (업로드는 기존 토큰으로도 계속 가능)
        from app.youtube.auth import token_path

        backup = token_path(settings).with_suffix(".json.bak")
        backup.write_text(token_path(settings).read_text(encoding="utf-8"), encoding="utf-8")
        print(f"기존 token.json 에 없는 권한을 요청합니다 → 브라우저 승인 (기존 토큰 백업: {backup.name})")
        service = build_service(settings, interactive=True, scopes=wanted)
    else:
        service = build_service(settings, interactive=True, scopes=wanted if not have else None)
    channels = service.channels().list(part="snippet", mine=True).execute()
    for item in channels.get("items", []):
        print(f"✅ 인증된 채널: {item['snippet']['title']} ({item['id']})")
    granted = token_scopes(settings)
    print(f"token.json 범위: {', '.join(s.rsplit('/', 1)[-1] for s in granted) or '(없음)'}")
    missing = [s.rsplit('/', 1)[-1] for s in wanted if s not in granted]
    if missing:
        print(f"⚠️  승인되지 않은 범위: {', '.join(missing)} (동의 화면에서 체크를 해제했거나 OAuth 앱 데이터 액세스에 범위가 없음)")
    print("GitHub Actions 를 쓰면 새 token.json 내용으로 Secret YOUTUBE_TOKEN_JSON 을 갱신하세요.")
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
    ai = settings.assets.ai_image
    line("AI 장면 이미지", ai.enabled, f"{ai.model} (무료 티어는 한도 0 → 결제 연결 필요)" if ai.enabled else "꺼짐 (assets.ai_image.enabled)", required=False)
    cf = settings.assets.cf_image
    cf_ok = bool(s.cloudflare_account_id and s.cloudflare_api_token and cf.enabled)
    line("Cloudflare AI 이미지", cf_ok, f"{cf.model} (무료, Gemini 이미지 실패 시 대체)" if cf_ok else "없음/꺼짐 (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN)", required=False)
    hf = settings.assets.hf_image
    line("HF_API_KEY", bool(s.hf_api_key and hf.enabled), f"{hf.model} (Gemini 이미지 실패 시 대체)" if s.hf_api_key and hf.enabled else "없음/꺼짐 → Gemini 실패 시 Pexels 로", required=False)
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
    from app.analytics.youtube_analytics import has_analytics_scope

    line("Analytics 권한", has_analytics_scope(settings),
         "yt-analytics.readonly 있음" if has_analytics_scope(settings) else "없음 → 성과 수집은 공개 통계만 (python -m app.main analytics status)",
         required=False)
    from app.youtube.comments import COMMENT_SCOPE, granted_scopes

    has_comment = COMMENT_SCOPE in granted_scopes(settings)
    line("댓글 권한", has_comment or not settings.comments.enabled,
         f"youtube.force-ssl {'있음' if has_comment else '없음'} / 자동 댓글 {'on' if settings.comments.enabled else 'off'}"
         + ("" if has_comment or not settings.comments.enabled else " → python -m app.main auth 후 YOUTUBE_TOKEN_JSON 갱신"),
         required=False)
    from app.voice.rights import voice_rights

    rights = voice_rights(settings)
    line("음성 상업 이용 권리", rights.ok, f"{rights.status} (python -m app.main rights)", required=False)
    line("트렌드", settings.trends.enabled,
         f"wikipedia={'on' if settings.trends.wikipedia.enabled else 'off'}, youtube={'on' if settings.trends.youtube.enabled else 'off'},"
         f" google_trends={'on' if settings.trends.google_trends.enabled else 'off(알파)'}", required=False)
    vm = settings.video_motion
    from app.video_motion.base import FreeStatus
    from app.video_motion.provider_factory import build_motion_providers

    motion_providers = build_motion_providers(settings) if vm.enabled else []
    verified = [p.name for p in motion_providers if p.free_status == FreeStatus.VERIFIED_FREE and p.configured]
    line("이미지→영상", True,
         ("off" if not vm.enabled else
          f"무료 검증 provider: {', '.join(verified) or '없음 → FFmpeg 움직임 효과만 (외부 호출 없음)'}"
          f" / 작업당 {vm.max_requests_per_job}회·하루 {vm.max_requests_per_day}회"), required=False)
    line("Hook 효과", True, f"{settings.video.hook_effect.preset} ({'on' if settings.video.hook_effect.enabled else 'off'})", required=False)
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

    p = sub.add_parser("topic", help="주제 후보 생성 + 중복 검사 + 점수 미리보기")
    p.add_argument("--category", help="카테고리 id 지정")
    p.add_argument("--no-trends", action="store_true", help="외부 트렌드 신호 없이 (기존 방식)")
    p.set_defaults(func=cmd_topic)

    p = sub.add_parser("trends", help="외부 트렌드 신호 미리보기")
    p.add_argument("--category", help="카테고리 id (기본: 첫 카테고리)")
    p.add_argument("--keyword", help="이 키워드의 관심도/과밀도 신호도 조회")
    p.set_defaults(func=cmd_trends)

    p = sub.add_parser("analytics", help="YouTube 성과 수집/보고서")
    asub = p.add_subparsers(dest="analytics_command", required=True)
    a = asub.add_parser("sync", help="업로드 영상 성과 수집 (Analytics API + 공개 통계)")
    a.add_argument("--days", type=int, help="게시 후 이 기간 안의 영상만 (기본 analytics.lookback_days)")
    a.add_argument("--video", help="특정 영상 ID 만")
    a = asub.add_parser("report", help="성과 보고서")
    a.add_argument("--days", type=int, default=None, help="최근 N일 업로드 영상 (기본 analytics.lookback_days)")
    a.add_argument("--format", choices=["text", "json", "csv"], default="text")
    a.add_argument("--output", help="json/csv 저장 경로 (기본 data/reports/performance.<format>)")
    a = asub.add_parser("status", help="token.json 의 Analytics 권한 점검")
    a = asub.add_parser("import-csv", help="Studio 전용 지표 CSV 가져오기 (피드 노출 / 시청 선택 비율)")
    a.add_argument("file")
    p.set_defaults(func=cmd_analytics)

    p = sub.add_parser("history", help="제작 이력")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("retry", help="실패 작업 재시도")
    p.add_argument("job_id", nargs="?")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("remake", help="완료된 작업을 지정 단계부터 다시 제작 + 업로드")
    p.add_argument("job_id", nargs="?", help="작업 ID (없으면 오늘 완료된 최근 작업)")
    p.add_argument("--from-step", default="voice", help="다시 시작할 단계 (기본 voice: 음성부터 다시)")
    p.set_defaults(func=cmd_remake)

    p = sub.add_parser("upload", help="제작 완료 영상 업로드")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--privacy", choices=["private", "scheduled", "public"], help="이번 업로드의 공개 방식 (기본: config)")
    p.add_argument("--approve-review", action="store_true",
                   help="콘텐츠 검수에서 보류(review_needed)된 작업을 사람이 확인했다고 기록하고 업로드")
    p.set_defaults(func=cmd_upload)

    p = sub.add_parser("comments", help="영상 자동 댓글 상태/재시도/수동 고정 기록")
    csub = p.add_subparsers(dest="comments_command", required=True)
    c = csub.add_parser("status", help="댓글 등록 상태 조회 (등록 상태와 고정 상태를 따로 표시)")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--verify", action="store_true", help="등록한 댓글이 아직 조회되는지 API 로 확인 (고정 여부는 확인 불가)")
    c = csub.add_parser("retry", help="대기/실패/권한 부족 댓글 재시도 (공개된 영상만, 같은 본문 재사용)")
    c.add_argument("job_id", nargs="?", help="특정 작업만")
    c.add_argument("--regenerate", action="store_true", help="검수 실패로 보류된 본문을 새로 생성 (작업 지정 시)")
    c.add_argument("--force", action="store_true", help="comments.enabled=false 여도 지정한 작업 하나는 처리")
    c.add_argument("--offline", action="store_true", help="Gemini 없이 대본의 댓글 질문으로 구성")
    c = csub.add_parser("preview", help="댓글 본문 미리보기 (저장/등록 안 함)")
    c.add_argument("job_id", nargs="?")
    c.add_argument("--offline", action="store_true", help="Gemini 없이 대본의 댓글 질문으로 구성")
    c = csub.add_parser("pins", help="고정 처리가 필요한 댓글 목록 (API 미지원 → 수동)")
    c.add_argument("--limit", type=int, default=50)
    c = csub.add_parser("mark-pinned", help="사람이 Studio 에서 고정했다고 기록")
    c.add_argument("target", help="영상 ID 또는 작업 ID")
    c.add_argument("--note", help="메모 (예: 'Studio 에서 고정')")
    c.add_argument("--undo", action="store_true", help="고정 기록 취소")
    c = csub.add_parser("guide", help="수동 고정 운영 가이드(Markdown) 생성")
    c.add_argument("--output", help="저장 경로 (기본 data/reports/pin_guide.md)")
    p.set_defaults(func=cmd_comments)

    p = sub.add_parser("review", help="콘텐츠 검수 결과 조회 (사실성/출처/장면/라이선스/광고 적합성)")
    p.add_argument("job_id", nargs="?", help="작업 ID (없으면 최근 검수 결과)")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("rights", help="음성(Edge TTS) 상업적 이용 권리 상태 점검")
    p.set_defaults(func=cmd_rights)

    p = sub.add_parser("branding", help="채널 브랜딩 자산/메타데이터 (미리보기 기본)")
    bsub = p.add_subparsers(dest="branding_command", required=True)
    bsub.add_parser("status", help="현재 채널 브랜딩 조회 (읽기만)")
    bsub.add_parser("check", help="홈페이지·약관·OAuth 범위·브랜드 파일 일관성 점검 (API 호출 없음)")
    bsub.add_parser("assets", help="brand.yaml 로 아이콘/배너 PNG 생성 (채널에 적용하지 않음)")
    b = bsub.add_parser("apply", help="brand.yaml 의 채널 소개/키워드 적용 (기본: 변경 미리보기만)")
    b.add_argument("--dry-run", action="store_true", help="미리보기만 (기본 동작)")
    b.add_argument("--yes", action="store_true", help="미리보기 내용을 실제 채널에 적용")
    b.add_argument("--banner", action="store_true", help="배너 이미지도 업로드 (channelBanners.insert)")
    p.set_defaults(func=cmd_branding)

    p = sub.add_parser("auth", help="YouTube OAuth 인증")
    p.add_argument("--no-analytics", action="store_true", help="Analytics 읽기 권한 없이 요청")
    p.add_argument("--no-comments", action="store_true", help="댓글/채널 관리 권한(youtube.force-ssl) 없이 요청")
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
