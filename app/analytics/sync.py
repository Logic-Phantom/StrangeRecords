"""업로드한 영상의 성과를 수집해 video_performance_snapshots 에 저장한다.

- 하루 1번 실행(GitHub Actions)을 전제로, 수집 시각과 게시 시각의 차이(hours_since_publish)를 그대로 기록한다.
  정확히 24시간 시점의 값처럼 꾸미지 않는다 → 보고서는 "24h 이후 첫 수집(실제 31.5h)" 처럼 실제 경과 시간을 표시.
- Analytics 값은 게시일 하루 전 ~ 오늘 범위 누적값 (API 는 집계가 끝난 날까지만 돌려주므로 1~3일 늦을 수 있음).
- 같은 영상·같은 수집일은 UPSERT (중복 저장 없음). 영상마다 실패를 격리하고, 실패한 영상은 다음 실행에서 다시 수집된다.
- Analytics 권한이 없으면 Data API 공개 통계만 저장하고(collection_status=no_scope) 재인증 방법을 안내한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.analytics.youtube_analytics import (
    METRIC_COLUMNS, REAUTH_GUIDE, AnalyticsPermissionError, AnalyticsScopeError, YouTubeAnalyticsClient,
    has_analytics_scope,
)
from app.config.settings import Settings
from app.database.db import Database
from app.database.repositories import PerformanceRepository, ProductionMetadataRepository, uploaded_jobs
from app.utils.logger import get_logger

logger = get_logger("analytics.sync")


@dataclass
class SyncSummary:
    enabled: bool = True
    scope_ok: bool = False
    checked: int = 0
    ok: int = 0
    partial: int = 0
    failed: int = 0
    not_found: int = 0
    no_data: int = 0
    messages: list[str] = field(default_factory=list)

    def line(self) -> str:
        return (f"성과 수집: 대상 {self.checked}개 → 완료 {self.ok}, 일부 {self.partial}, 데이터 없음 {self.no_data},"
                f" 실패 {self.failed}, 영상 없음 {self.not_found} (Analytics 권한 {'있음' if self.scope_ok else '없음'})")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class AnalyticsSync:
    def __init__(self, settings: Settings, db: Database, client: YouTubeAnalyticsClient | None = None,
                 now: datetime | None = None):
        self.settings = settings
        self.cfg = settings.analytics
        self.db = db
        self.client = client or YouTubeAnalyticsClient(settings)
        self.perf = PerformanceRepository(db)
        self.prod = ProductionMetadataRepository(db, settings.app.timezone)
        self.tz = ZoneInfo(settings.app.timezone)
        self._now = now

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(self.tz)

    def run(self, days: int | None = None, video_id: str | None = None) -> SyncSummary:
        summary = SyncSummary(enabled=self.cfg.enabled)
        if not self.cfg.enabled:
            summary.messages.append("analytics.enabled=false → 건너뜀")
            return summary
        since = (self.now - timedelta(days=days or self.cfg.lookback_days)).isoformat(timespec="seconds")
        jobs = uploaded_jobs(self.db, since)
        if video_id:
            jobs = [j for j in jobs if j.youtube_video_id == video_id]
        jobs = jobs[: self.cfg.max_videos_per_run]
        summary.checked = len(jobs)
        if not jobs:
            summary.messages.append("수집할 업로드 영상 없음")
            return summary

        summary.scope_ok = has_analytics_scope(self.settings) or self.client._analytics is not None
        if not summary.scope_ok:
            summary.messages.append(REAUTH_GUIDE)
            logger.warning(REAUTH_GUIDE)

        ids = [j.youtube_video_id for j in jobs if j.youtube_video_id]
        try:
            details = self.client.video_details(ids)
        except Exception as exc:
            logger.warning("Data API 영상 정보 조회 실패 (공개 통계 없이 진행): %s", exc)
            summary.messages.append(f"videos.list 실패: {exc}")
            details = {}

        analytics_ok = summary.scope_ok
        for job in jobs:
            vid = job.youtube_video_id or ""
            try:
                status = self._collect_one(job.id, vid, details.get(vid), analytics_ok, bool(details))
            except (AnalyticsScopeError, AnalyticsPermissionError) as exc:
                # 권한 문제는 영상마다 같으므로 나머지는 공개 통계만 수집
                analytics_ok = False
                summary.messages.append(str(exc)[:300])
                logger.warning("Analytics 사용 불가 → 공개 통계만 수집: %s", exc)
                status = self._collect_one(job.id, vid, details.get(vid), False, bool(details))
            except Exception as exc:
                logger.warning("성과 수집 실패 (%s): %s", vid, exc)
                self._store(job.id, vid, details.get(vid), {}, [], "failed", f"{type(exc).__name__}: {exc}")
                status = "failed"
            if status == "ok":
                summary.ok += 1
            elif status in ("partial", "no_scope"):
                summary.partial += 1
            elif status == "not_found":
                summary.not_found += 1
            elif status == "no_data":
                summary.no_data += 1
            else:
                summary.failed += 1
        logger.info(summary.line())
        return summary

    def _collect_one(self, job_id: str, vid: str, detail: dict | None, use_analytics: bool, details_loaded: bool) -> str:
        if details_loaded and detail is None:
            # 삭제/비공개 전환 등으로 조회되지 않음
            self._store(job_id, vid, None, {}, [], "not_found", "videos.list 결과 없음 (삭제 또는 접근 불가)")
            return "not_found"
        values: dict = {}
        missing: list[str] = []
        error = None
        raw: dict = {}
        if use_analytics:
            published = _parse((detail or {}).get("published_at"))
            start = ((published or self.now) - timedelta(days=1)).date().isoformat()
            end = self.now.date().isoformat()
            result = self.client.video_metrics(vid, start, end, self.cfg.metrics)
            values = result.values
            missing = result.missing
            raw = {"analytics": result.raw, "errors": result.errors, "report_start_date": start, "report_end_date": end}
            if result.errors:
                error = "; ".join(result.errors)[:1000]
            if result.no_data:
                status = "no_data"
            else:
                status = "ok" if not missing else "partial"
        else:
            missing = list(self.cfg.metrics)
            status = "no_scope" if detail else "failed"
        self._store(job_id, vid, detail, values, missing, status, error, raw)
        return status

    def _store(self, job_id: str, vid: str, detail: dict | None, values: dict, missing: list[str], status: str,
               error: str | None, raw: dict | None = None) -> None:
        detail = detail or {}
        now = self.now
        published = _parse(detail.get("published_at"))
        hours = round((now - published).total_seconds() / 3600, 2) if published else None
        raw = raw or {}
        snapshot = {
            "job_id": job_id, "youtube_video_id": vid,
            "collected_at": now.isoformat(timespec="seconds"), "collected_date": now.date().isoformat(),
            "published_at": published.isoformat() if published else None, "hours_since_publish": hours,
            "report_start_date": raw.get("report_start_date"), "report_end_date": raw.get("report_end_date"),
            "public_view_count": detail.get("public_view_count"), "public_like_count": detail.get("public_like_count"),
            "public_comment_count": detail.get("public_comment_count"), "duration_seconds": detail.get("duration_seconds"),
            "source": "youtube_analytics+data_api" if values else ("youtube_data_api" if detail else "none"),
            "collection_status": status, "missing_metrics": missing, "error": error,
            "raw_json": {k: v for k, v in raw.items() if k != "analytics"} | {"privacy_status": detail.get("privacy_status")},
        }
        for metric, column in METRIC_COLUMNS.items():
            snapshot[column] = values.get(metric)
        self.perf.upsert_snapshot(snapshot)
        if vid and self.prod.get(job_id):
            try:
                with self.db.session() as conn:
                    conn.execute("UPDATE production_metadata SET youtube_video_id = ? WHERE job_id = ? AND youtube_video_id IS NULL",
                                 (vid, job_id))
            except Exception:
                pass
