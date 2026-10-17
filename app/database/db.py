"""SQLite 연결 및 스키마."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT,
    title         TEXT NOT NULL,
    category      TEXT NOT NULL,
    topic         TEXT NOT NULL,
    keywords      TEXT NOT NULL DEFAULT '[]',
    summary       TEXT NOT NULL DEFAULT '',
    truth_status  TEXT NOT NULL DEFAULT 'UNCONFIRMED',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    video_id      TEXT,
    youtube_url   TEXT,
    status        TEXT NOT NULL DEFAULT 'selected'
);
CREATE INDEX IF NOT EXISTS idx_topics_category ON topics(category);
CREATE INDEX IF NOT EXISTS idx_topics_status ON topics(status);

CREATE TABLE IF NOT EXISTS jobs (
    id                 TEXT PRIMARY KEY,
    mode               TEXT NOT NULL,
    upload_enabled     INTEGER NOT NULL DEFAULT 0,
    topic_id           INTEGER REFERENCES topics(id),
    status             TEXT NOT NULL,
    current_step       TEXT,
    completed_steps    TEXT NOT NULL DEFAULT '[]',
    state_json         TEXT NOT NULL DEFAULT '{}',
    error              TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    video_path         TEXT,
    youtube_video_id   TEXT,
    youtube_url        TEXT,
    publish_at         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    uploaded_at        TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS assets (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           TEXT NOT NULL,
    scene_number     INTEGER NOT NULL,
    media_type       TEXT NOT NULL,
    source           TEXT NOT NULL,
    source_id        TEXT,
    source_url       TEXT,
    author           TEXT,
    license          TEXT,
    local_path       TEXT,
    generated_by_ai  INTEGER NOT NULL DEFAULT 0,
    downloaded_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_assets_job ON assets(job_id);

CREATE TABLE IF NOT EXISTS step_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL,
    step        TEXT NOT NULL,
    status      TEXT NOT NULL,
    message     TEXT,
    duration_s  REAL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_step_logs_job ON step_logs(job_id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TEXT NOT NULL
);
"""

# 기존 DB(이전 버전)에도 안전하게 적용되는 순차 마이그레이션.
# 이미 적용된 버전은 건너뛰고, 새 테이블만 추가한다 (기존 테이블/데이터는 건드리지 않음).
MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "growth_engine", """
-- YouTube 성과 스냅샷: 수집일(KST) 하루에 영상당 1행 (같은 날 다시 수집하면 UPSERT 로 갱신)
-- 값이 NULL 이면 "수집하지 못함" (0 과 구분). 기간 값(report_*)은 게시일~report_end_date 누적값이다.
CREATE TABLE IF NOT EXISTS video_performance_snapshots (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                     TEXT,
    youtube_video_id           TEXT NOT NULL,
    collected_at               TEXT NOT NULL,
    collected_date             TEXT NOT NULL,
    published_at               TEXT,
    hours_since_publish        REAL,
    report_start_date          TEXT,
    report_end_date            TEXT,
    views                      INTEGER,
    engaged_views              INTEGER,
    estimated_minutes_watched  REAL,
    average_view_duration      REAL,
    average_view_percentage    REAL,
    likes                      INTEGER,
    comments                   INTEGER,
    shares                     INTEGER,
    subscribers_gained         INTEGER,
    subscribers_lost           INTEGER,
    public_view_count          INTEGER,
    public_like_count          INTEGER,
    public_comment_count       INTEGER,
    duration_seconds           REAL,
    source                     TEXT NOT NULL,
    collection_status          TEXT NOT NULL,
    missing_metrics            TEXT NOT NULL DEFAULT '[]',
    error                      TEXT,
    raw_json                   TEXT NOT NULL DEFAULT '{}',
    UNIQUE (youtube_video_id, collected_date)
);
CREATE INDEX IF NOT EXISTS idx_perf_video ON video_performance_snapshots(youtube_video_id);
CREATE INDEX IF NOT EXISTS idx_perf_job ON video_performance_snapshots(job_id);

-- YouTube Studio 전용 지표 (API 미제공: 피드 노출수, 시청 선택 비율 등) - CSV/수동 가져오기만, 출처 기록 필수
CREATE TABLE IF NOT EXISTS studio_metrics (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id   TEXT NOT NULL,
    metric             TEXT NOT NULL,
    value              REAL NOT NULL,
    report_start_date  TEXT NOT NULL DEFAULT '',
    report_end_date    TEXT NOT NULL DEFAULT '',
    source             TEXT NOT NULL,
    imported_at        TEXT NOT NULL,
    UNIQUE (youtube_video_id, metric, report_start_date, report_end_date)
);

-- 외부 트렌드 신호 (같은 날 같은 provider/키워드는 캐시로 재사용)
CREATE TABLE IF NOT EXISTS trend_signals (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    provider             TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    keyword              TEXT NOT NULL,
    category             TEXT,
    region               TEXT,
    source_url           TEXT,
    collected_at         TEXT NOT NULL,
    collected_date       TEXT NOT NULL,
    period_start         TEXT,
    period_end           TEXT,
    interest             REAL,
    interest_normalized  REAL,
    velocity             REAL,
    video_count          INTEGER,
    total_views          INTEGER,
    views_per_hour       REAL,
    published_at         TEXT,
    related_terms        TEXT NOT NULL DEFAULT '[]',
    confidence           REAL,
    status               TEXT NOT NULL DEFAULT 'ok',
    error                TEXT,
    raw_json             TEXT NOT NULL DEFAULT '{}',
    UNIQUE (provider, kind, keyword, region, collected_date)
);
CREATE INDEX IF NOT EXISTS idx_trend_date ON trend_signals(collected_date);

-- 주제 후보 점수와 선정 근거
CREATE TABLE IF NOT EXISTS topic_scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT,
    title         TEXT NOT NULL,
    topic         TEXT NOT NULL,
    category      TEXT NOT NULL,
    selected      INTEGER NOT NULL DEFAULT 0,
    duplicate     INTEGER NOT NULL DEFAULT 0,
    final_score   REAL,
    components    TEXT NOT NULL DEFAULT '{}',
    weights       TEXT NOT NULL DEFAULT '{}',
    reasons       TEXT NOT NULL DEFAULT '[]',
    trend_sources TEXT NOT NULL DEFAULT '[]',
    strategy      TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topic_scores_job ON topic_scores(job_id);

-- 영상별 제작 조건 (Analytics 성과와 연결해 어떤 조건이 좋았는지 비교)
CREATE TABLE IF NOT EXISTS production_metadata (
    job_id                   TEXT PRIMARY KEY,
    youtube_video_id         TEXT,
    topic_category           TEXT,
    topic_score              REAL,
    trend_sources            TEXT NOT NULL DEFAULT '[]',
    hook_type                TEXT,
    story_structure          TEXT,
    loop_ending_enabled      INTEGER NOT NULL DEFAULT 0,
    loop_quality_score       REAL,
    visual_style             TEXT,
    hook_effect_preset       TEXT,
    video_duration           REAL,
    subtitle_mode            TEXT,
    title_strategy           TEXT,
    script_similarity_score  REAL,
    originality_score        REAL,
    extra_json               TEXT NOT NULL DEFAULT '{}',
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prodmeta_video ON production_metadata(youtube_video_id);
"""),
    (2, "video_comments", """
-- 영상별 자동 댓글 (영상당 1행 → 같은 영상에 두 번 등록하지 않음)
-- status: pending | waiting_publish | posted | failed | permission_denied | rejected | skipped
-- pin_status: not_applicable(등록 전) | manual_required(API 미지원, 사람이 고정 필요) | pinned_manual(사람이 고정했다고 기록) | unknown(확인 불가)
CREATE TABLE IF NOT EXISTS video_comments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT,
    youtube_video_id  TEXT NOT NULL UNIQUE,
    comment_text      TEXT NOT NULL DEFAULT '',
    comment_type      TEXT NOT NULL DEFAULT '',
    generator         TEXT NOT NULL DEFAULT '',
    comment_id        TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    attempts          INTEGER NOT NULL DEFAULT 0,
    error_code        TEXT,
    error_message     TEXT,
    generated_at      TEXT,
    posted_at         TEXT,
    last_attempt_at   TEXT,
    pin_status        TEXT NOT NULL DEFAULT 'not_applicable',
    pin_recorded_at   TEXT,
    pin_note          TEXT,
    extra_json        TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_job ON video_comments(job_id);
CREATE INDEX IF NOT EXISTS idx_comments_status ON video_comments(status);
"""),
    (3, "video_motion", """
-- 장면별 이미지→영상 결과 (작업·장면당 1행, 재실행 시 갱신)
-- status: ai_video_generated | cache_reused | fallback_motion | quota_limited | provider_disabled | generation_failed
CREATE TABLE IF NOT EXISTS video_motion_clips (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             TEXT NOT NULL,
    scene_number       INTEGER NOT NULL,
    priority           INTEGER,
    image_path         TEXT,
    image_sha256       TEXT,
    video_path         TEXT,
    provider           TEXT,
    model              TEXT,
    cache_key          TEXT,
    requested_at       TEXT,
    status             TEXT NOT NULL,
    success            INTEGER NOT NULL DEFAULT 0,
    clip_seconds       REAL,
    latency_s          REAL,
    error_code         TEXT,
    stop_reason        TEXT,
    quota_limited      INTEGER NOT NULL DEFAULT 0,
    fallback_applied   INTEGER NOT NULL DEFAULT 0,
    fallback_motion    TEXT,
    cache_reused       INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE (job_id, scene_number)
);
CREATE INDEX IF NOT EXISTS idx_motion_clips_key ON video_motion_clips(cache_key);

-- 실제로 보낸 외부 Image-to-Video 요청 (하루 요청 한도 계산용, 실패한 요청도 기록)
CREATE TABLE IF NOT EXISTS video_motion_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT,
    scene_number   INTEGER,
    provider       TEXT NOT NULL,
    model          TEXT,
    requested_at   TEXT NOT NULL,
    request_date   TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    http_status    INTEGER,
    latency_s      REAL
);
CREATE INDEX IF NOT EXISTS idx_motion_requests_date ON video_motion_requests(request_date);
"""),
]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.session() as conn:
            conn.executescript(SCHEMA)
        self.migrate()

    def schema_version(self) -> int:
        with self.session() as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        return int(row[0] or 0)

    def migrate(self) -> list[int]:
        """적용되지 않은 마이그레이션만 순서대로 적용한다. 반환: 이번에 적용한 버전 목록."""
        from datetime import datetime, timezone

        current = self.schema_version()
        applied: list[int] = []
        for version, name, sql in MIGRATIONS:
            if version <= current:
                continue
            with self.session() as conn:
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
            applied.append(version)
        return applied
