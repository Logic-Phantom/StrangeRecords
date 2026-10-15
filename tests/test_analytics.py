"""YouTube Analytics 성과 수집 / SQLite 마이그레이션 / 보고서 / 성과 반영 (실제 API 호출 없음)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.analytics.insights import PerformanceInsights
from app.analytics.report import build_report, checkpoint_values, export_report, format_report
from app.analytics.studio_import import import_studio_csv
from app.analytics.sync import AnalyticsSync
from app.analytics.youtube_analytics import (
    ANALYTICS_SCOPE, AnalyticsPermissionError, AnalyticsScopeError, YouTubeAnalyticsClient, has_analytics_scope,
    parse_iso_duration,
)
from app.database.db import MIGRATIONS, SCHEMA, Database
from app.database.models import JobStatus
from app.database.repositories import JobRepository, PerformanceRepository, ProductionMetadataRepository

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 10, 20, 22, 30, tzinfo=KST)


class HttpErr(Exception):
    def __init__(self, status, msg="err"):
        super().__init__(msg)
        self.resp = type("R", (), {"status": status})()


class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self, num_retries=0):
        return self.fn()


class FakeAnalytics:
    """reports().query(...) 를 흉내낸다. reject: 이 지표 조합이 들어오면 400."""

    def __init__(self, values=None, reject=None, status=None, empty=False):
        self.values = values or {}
        self.reject = reject or set()
        self.status = status
        self.empty = empty
        self.queries: list[dict] = []

    def reports(self):
        outer = self

        class R:
            def query(self, **kw):
                outer.queries.append(kw)

                def run():
                    metrics = kw["metrics"].split(",")
                    if outer.status:
                        raise HttpErr(outer.status)
                    if len(metrics) > 1 and outer.reject & set(metrics):
                        raise HttpErr(400, "incompatible metrics")
                    if metrics == ["engagedViews"] and "engagedViews" in outer.reject:
                        raise HttpErr(400, "unknown metric")
                    if outer.empty:
                        return {"columnHeaders": [{"name": m} for m in metrics], "rows": []}
                    return {"columnHeaders": [{"name": m} for m in metrics], "rows": [[outer.values.get(m, 0) for m in metrics]]}
                return Req(run)
        return R()


class FakeData:
    def __init__(self, items):
        self.items = items

    def videos(self):
        outer = self

        class V:
            def list(self, **kw):
                ids = kw["id"].split(",")
                return Req(lambda: {"items": [i for i in outer.items if i["id"] in ids]})
        return V()


def video_item(vid, published, views=100):
    return {"id": vid, "snippet": {"title": vid, "publishedAt": published}, "contentDetails": {"duration": "PT52S"},
            "status": {"privacyStatus": "public"}, "statistics": {"viewCount": str(views), "likeCount": "5", "commentCount": "1"}}


METRICS = {"views": 1200, "engagedViews": 900, "estimatedMinutesWatched": 600, "averageViewDuration": 30,
           "averageViewPercentage": 61.5, "likes": 40, "comments": 3, "shares": 7, "subscribersGained": 4, "subscribersLost": 1}


def uploaded_job(db, job_id, vid, category="unsolved_mystery", hook_type="anomaly", uploaded=NOW - timedelta(days=3)):
    jobs = JobRepository(db)
    jobs.create(job_id, "production", True)
    state = {"topic": {"category": category, "title": job_id}, "metadata": {"title": f"제목 {job_id}"},
             "draft": {"hook_type": hook_type, "loop": False}, "render": {"duration": 50.0}}
    jobs.save_state(job_id, [], state, None)
    jobs.update(job_id, status=JobStatus.SUCCESS, youtube_video_id=vid, youtube_url=f"https://youtube.com/shorts/{vid}",
                uploaded_at=uploaded.isoformat())


# ------------------------------------------------------------------ client
def test_video_metrics_single_request(settings):
    fake = FakeAnalytics(METRICS)
    r = YouTubeAnalyticsClient(settings, analytics_service=fake, data_service=FakeData([])).video_metrics("vid", "2026-10-01", "2026-10-20")
    assert r.values["engagedViews"] == 900 and not r.missing and len(fake.queries) == 1
    assert fake.queries[0]["filters"] == "video==vid" and fake.queries[0]["ids"] == "channel==MINE"  # 영상 필터 정확히


def test_incompatible_metrics_are_split_and_missing_recorded(settings):
    fake = FakeAnalytics(METRICS, reject={"engagedViews"})
    r = YouTubeAnalyticsClient(settings, analytics_service=fake).video_metrics("vid", "2026-10-01", "2026-10-20")
    assert r.values["views"] == 1200 and r.values["shares"] == 7
    assert r.missing == ["engagedViews"] and r.values["engagedViews"] is None and r.errors
    assert len(fake.queries) == 4  # 전체 1회 + 묶음 3회


def test_permission_error_and_no_data(settings):
    with pytest.raises(AnalyticsPermissionError):
        YouTubeAnalyticsClient(settings, analytics_service=FakeAnalytics(status=403)).video_metrics("v", "a", "b")
    r = YouTubeAnalyticsClient(settings, analytics_service=FakeAnalytics(empty=True)).video_metrics("v", "a", "b")
    assert r.no_data and all(v is None for v in r.values.values())  # 데이터 없음을 0 으로 만들지 않음


def test_missing_scope_guides_reauth(settings):
    (settings.paths.root / "token.json").write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/youtube.upload"]}))
    assert not has_analytics_scope(settings)
    with pytest.raises(AnalyticsScopeError, match="python -m app.main auth"):
        _ = YouTubeAnalyticsClient(settings).analytics


def test_parse_duration():
    assert parse_iso_duration("PT52S") == 52 and parse_iso_duration("PT1M3S") == 63 and parse_iso_duration(None) is None


# -------------------------------------------------------------------- sync
def test_sync_stores_snapshot_with_elapsed_hours_and_upserts(settings, db):
    uploaded_job(db, "j1", "vid1")
    published = (NOW - timedelta(hours=31.5)).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = YouTubeAnalyticsClient(settings, analytics_service=FakeAnalytics(METRICS), data_service=FakeData([video_item("vid1", published)]))
    summary = AnalyticsSync(settings, db, client, now=NOW).run()
    assert summary.ok == 1
    AnalyticsSync(settings, db, client, now=NOW + timedelta(hours=1)).run()  # 같은 날 다시 수집
    rows = PerformanceRepository(db).snapshots("vid1")
    assert len(rows) == 1  # 같은 영상·같은 수집일 중복 저장 없음
    row = rows[0]
    assert row["hours_since_publish"] == pytest.approx(32.5, abs=0.01)  # 실제 경과 시간 그대로
    assert row["views"] == 1200 and row["public_view_count"] == 100  # Analytics 값과 공개 통계 구분
    assert row["source"] == "youtube_analytics+data_api" and row["collection_status"] == "ok"
    assert row["report_start_date"] and row["report_end_date"] == (NOW + timedelta(hours=1)).date().isoformat()


def test_sync_without_scope_keeps_public_stats(settings, db):
    uploaded_job(db, "j1", "vid1")
    client = YouTubeAnalyticsClient(settings, data_service=FakeData([video_item("vid1", "2026-10-18T13:00:00Z")]))
    summary = AnalyticsSync(settings, db, client, now=NOW).run()
    row = PerformanceRepository(db).snapshots("vid1")[0]
    assert summary.partial == 1 and not summary.scope_ok and any("auth" in m for m in summary.messages)
    assert row["collection_status"] == "no_scope" and row["views"] is None and row["public_view_count"] == 100
    assert "averageViewPercentage" in row["missing_metrics"]


def test_sync_isolates_failures_and_deleted_videos(settings, db):
    uploaded_job(db, "j1", "gone")
    uploaded_job(db, "j2", "vid2")

    class Boom(FakeAnalytics):
        def reports(self):
            raise RuntimeError("network down")

    client = YouTubeAnalyticsClient(settings, analytics_service=Boom(), data_service=FakeData([video_item("vid2", "2026-10-18T13:00:00Z")]))
    summary = AnalyticsSync(settings, db, client, now=NOW).run()
    assert summary.not_found == 1 and summary.failed == 1  # 다음 실행에서 다시 수집됨
    assert PerformanceRepository(db).snapshots("vid2")[0]["collection_status"] == "failed"


def test_sync_disabled(settings, db):
    settings.analytics.enabled = False
    assert AnalyticsSync(settings, db, YouTubeAnalyticsClient(settings), now=NOW).run().checked == 0


# --------------------------------------------------------------- migration
def test_migration_on_old_database_keeps_data(tmp_path):
    path = tmp_path / "old.sqlite"
    old_schema = SCHEMA.split("CREATE TABLE IF NOT EXISTS schema_migrations")[0]
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute("INSERT INTO topics (title, category, topic, created_at, updated_at) VALUES ('t', 'c', 'x', 'n', 'n')")
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.schema_version() == MIGRATIONS[-1][0]
    with db.session() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert c.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    assert {"video_performance_snapshots", "trend_signals", "topic_scores", "production_metadata", "studio_metrics"} <= tables
    assert Database(path).migrate() == []  # 다시 열어도 재적용 없음


# ------------------------------------------------------------------ report
def _snap(db, vid, job, hours, collected, **metrics):
    PerformanceRepository(db).upsert_snapshot({
        "job_id": job, "youtube_video_id": vid, "collected_at": collected.isoformat(), "collected_date": collected.date().isoformat(),
        "published_at": (collected - timedelta(hours=hours)).isoformat(), "hours_since_publish": hours,
        "source": "youtube_analytics+data_api", "collection_status": "ok", **metrics,
    })


def test_checkpoints_report_actual_elapsed_hours():
    snaps = [{"hours_since_publish": h, "collection_status": "ok", "views": v, "collected_at": f"t{h}"} for h, v in
             ((5, 10), (29, 100), (77, 300), (180, 500))]
    cps = checkpoint_values(snaps, [24, 72, 168])
    assert cps["24h"]["actual_hours"] == 29 and cps["72h"]["views"] == 300 and cps["7d"]["actual_hours"] == 180


def _populate(db, settings):
    data = [("j1", "v1", "anomaly", 70.0, 30, 2000), ("j2", "v2", "anomaly", 65.0, 20, 1500), ("j3", "v3", "question", 40.0, 2, 300),
            ("j4", "v4", "question", 45.0, 1, 400)]
    for job, vid, hook, avp, shares, views in data:
        uploaded_job(db, job, vid, hook_type=hook, uploaded=NOW - timedelta(days=5))
        ProductionMetadataRepository(db).upsert(job, youtube_video_id=vid, topic_category="unsolved_mystery", hook_type=hook,
                                                video_duration=50.0, hook_effect_preset="subtle")
        _snap(db, vid, job, 80, NOW - timedelta(days=2), views=views, average_view_percentage=avp, shares=shares,
              subscribers_gained=3, subscribers_lost=0)
        _snap(db, vid, job, 128, NOW, views=views + 100, average_view_percentage=avp, shares=shares,
              subscribers_gained=3, subscribers_lost=0)


def test_report_groups_diagnosis_and_export(settings, db, tmp_path):
    _populate(db, settings)
    report = build_report(db, settings, now=NOW)
    assert report["ready_count"] == 4
    hooks = {g["value"]: g for g in report["groups"]["hook_type"]}
    assert hooks["anomaly"]["n"] == 2 and hooks["anomaly"]["confidence"].startswith("판단 불가")  # 표본 수 표시
    v3 = next(r for r in report["videos"] if r["video_id"] == "v3")
    assert {d["code"] for d in v3["diagnosis"]} & {"low_retention", "low_views"}
    assert v3["checkpoints"]["72h"]["actual_hours"] == 80 and v3["views_72h"] == 300
    text = format_report(report)
    assert "아직 판단할 수 없는 항목" in text and "Shown in feed" in text
    assert export_report(report, tmp_path / "r.csv", "csv").read_text(encoding="utf-8-sig").startswith("job_id")
    assert json.loads(export_report(report, tmp_path / "r.json", "json").read_text())["ready_count"] == 4


def test_report_with_no_data_is_explicit(settings, db):
    uploaded_job(db, "j1", "v1", uploaded=NOW - timedelta(hours=5))
    report = build_report(db, settings, now=NOW)
    assert report["ready_count"] == 0 and report["videos"][0]["diagnosis"][0]["code"] == "insufficient_data"
    assert "기존 제작 기준을 유지" in report["suggestions"][0]


def test_insights_need_minimum_samples_and_shrink(settings, db):
    uploaded_job(db, "j1", "v1", uploaded=NOW - timedelta(days=5))
    _snap(db, "v1", "j1", 100, NOW, views=10, average_view_percentage=10)
    few = PerformanceInsights(db, settings, now=NOW)
    assert few.category_prior("unsolved_mystery") is None and "데이터 부족" in few.prompt_context()

    db2 = Database(settings.paths.data / "other.sqlite")
    _populate(db2, settings)
    ins = PerformanceInsights(db2, settings, now=NOW)
    prior = ins.category_prior("unsolved_mystery")
    assert 0.4 < prior < 0.6  # 채널 평균(0.5) 쪽으로 축소
    assert ins.category_prior("brain_teaser") == pytest.approx(0.5)
    assert "신뢰도" in ins.prompt_context("unsolved_mystery")


# --------------------------------------------------------------- studio csv
def test_studio_csv_import_records_source(db, tmp_path):
    csv_path = tmp_path / "studio.csv"
    csv_path.write_text("Content,Shown in feed,Viewed (vs. swiped away) (%),Other\nTotal,1000,70,x\nabcdefghijk,800,65.5%,y\n", encoding="utf-8")
    repo = PerformanceRepository(db)
    result = import_studio_csv(csv_path, repo)
    assert result.stored == 2 and result.skipped == ["Total"] and "Other" in result.ignored_columns
    metrics = repo.studio_metrics()["abcdefghijk"]
    assert metrics["viewed_rate"]["value"] == 65.5 and metrics["shown_in_feed"]["source"] == "manual_csv:studio.csv"
    import_studio_csv(csv_path, repo)  # 다시 가져와도 중복 없음
    with db.session() as c:
        assert c.execute("SELECT COUNT(*) FROM studio_metrics").fetchone()[0] == 2


# -------------------------------------------------------------------- auth
def test_load_credentials_keeps_token_scopes(settings):
    from app.youtube.auth import SCOPES, load_credentials

    token = {"token": "x", "refresh_token": "r", "client_id": "c", "client_secret": "s",
             "token_uri": "https://oauth2.googleapis.com/token", "expiry": "2099-01-01T00:00:00Z",
             "scopes": [*SCOPES, ANALYTICS_SCOPE]}
    (settings.paths.root / "token.json").write_text(json.dumps(token))
    creds = load_credentials(settings)
    assert ANALYTICS_SCOPE in creds.scopes and set(SCOPES) <= set(creds.scopes)  # 갱신 저장 시 Analytics 범위 유지
    token["scopes"] = SCOPES
    (settings.paths.root / "token.json").write_text(json.dumps(token))
    assert ANALYTICS_SCOPE not in load_credentials(settings).scopes  # 받지 않은 범위를 요청하지 않음
