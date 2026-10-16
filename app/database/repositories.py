"""topics / jobs / assets / step_logs 저장소."""

from __future__ import annotations

import json
from typing import Any, Iterable

from app.database.db import Database
from app.database.models import JobRecord, JobStatus, TopicRecord, TopicStatus
from app.schemas import AssetInfo, TopicCandidate
from app.utils.files import now_iso


class TopicRepository:
    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def add(self, candidate: TopicCandidate, job_id: str | None = None, status: str = TopicStatus.SELECTED) -> int:
        now = now_iso(self.tz)
        with self.db.session() as conn:
            cur = conn.execute(
                """INSERT INTO topics (job_id, title, category, topic, keywords, summary, truth_status,
                                       created_at, updated_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job_id, candidate.title, candidate.category, candidate.topic,
                    json.dumps(candidate.keywords, ensure_ascii=False), candidate.summary,
                    candidate.truth_status, now, now, status,
                ),
            )
            return int(cur.lastrowid)

    def get(self, topic_id: int) -> TopicRecord | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return TopicRecord.from_row(row) if row else None

    def history(self, limit: int = 300, include_discarded: bool = False) -> list[TopicRecord]:
        query = "SELECT * FROM topics"
        if not include_discarded:
            query += f" WHERE status != '{TopicStatus.DISCARDED}'"
        query += " ORDER BY id DESC LIMIT ?"
        with self.db.session() as conn:
            rows = conn.execute(query, (limit,)).fetchall()
        return [TopicRecord.from_row(r) for r in rows]

    def last_used_by_category(self) -> dict[str, str]:
        with self.db.session() as conn:
            rows = conn.execute(
                f"SELECT category, MAX(created_at) AS last FROM topics WHERE status != '{TopicStatus.DISCARDED}' GROUP BY category"
            ).fetchall()
        return {r["category"]: r["last"] for r in rows}

    def update(self, topic_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso(self.tz)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.session() as conn:
            conn.execute(f"UPDATE topics SET {assignments} WHERE id = ?", (*fields.values(), topic_id))


class JobRepository:
    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def create(self, job_id: str, mode: str, upload_enabled: bool) -> JobRecord:
        now = now_iso(self.tz)
        with self.db.session() as conn:
            conn.execute(
                """INSERT INTO jobs (id, mode, upload_enabled, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, mode, int(upload_enabled), JobStatus.RUNNING, now, now),
            )
        job = self.get(job_id)
        assert job is not None
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobRecord.from_row(row) if row else None

    def save_state(self, job_id: str, completed_steps: Iterable[str], state: dict[str, Any], current_step: str | None) -> None:
        self.update(
            job_id,
            completed_steps=json.dumps(list(completed_steps)),
            state_json=json.dumps(state, ensure_ascii=False, default=str),
            current_step=current_step,
        )

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso(self.tz)
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.session() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))

    def latest(self, statuses: Iterable[str] | None = None) -> JobRecord | None:
        query, params = "SELECT * FROM jobs", []
        statuses = list(statuses or [])
        if statuses:
            query += f" WHERE status IN ({','.join('?' * len(statuses))})"
            params = statuses
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        with self.db.session() as conn:
            row = conn.execute(query, params).fetchone()
        return JobRecord.from_row(row) if row else None

    def list(self, limit: int = 20) -> list[JobRecord]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [JobRecord.from_row(r) for r in rows]

    def count_successful_since(self, since_iso: str, upload_only: bool = True) -> int:
        query = "SELECT COUNT(*) FROM jobs WHERE created_at >= ? AND status = ?"
        if upload_only:
            query += " AND upload_enabled = 1"
        with self.db.session() as conn:
            return int(conn.execute(query, (since_iso, JobStatus.SUCCESS)).fetchone()[0])


class AssetRepository:
    def __init__(self, db: Database):
        self.db = db

    def replace_for_job(self, job_id: str, assets: Iterable[AssetInfo]) -> None:
        with self.db.session() as conn:
            conn.execute("DELETE FROM assets WHERE job_id = ?", (job_id,))
            conn.executemany(
                """INSERT INTO assets (job_id, scene_number, media_type, source, source_id, source_url, author,
                                       license, local_path, generated_by_ai, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        job_id, a.scene_number, a.media_type, a.source, a.source_id, a.source_url, a.author,
                        a.license, a.local_path, int(a.generated_by_ai), a.downloaded_at,
                    )
                    for a in assets
                ],
            )

    def used_source_ids(self, source: str) -> set[str]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT source_id FROM assets WHERE source = ?", (source,)).fetchall()
        return {r["source_id"] for r in rows if r["source_id"]}


class StepLogRepository:
    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def add(self, job_id: str, step: str, status: str, message: str = "", duration_s: float | None = None) -> None:
        with self.db.session() as conn:
            conn.execute(
                "INSERT INTO step_logs (job_id, step, status, message, duration_s, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (job_id, step, status, message[:2000], duration_s, now_iso(self.tz)),
            )


# ====================================================================== growth engine
_PERF_METRIC_COLUMNS = (
    "views", "engaged_views", "estimated_minutes_watched", "average_view_duration", "average_view_percentage",
    "likes", "comments", "shares", "subscribers_gained", "subscribers_lost",
)
_PERF_COLUMNS = (
    "job_id", "youtube_video_id", "collected_at", "collected_date", "published_at", "hours_since_publish",
    "report_start_date", "report_end_date", *_PERF_METRIC_COLUMNS,
    "public_view_count", "public_like_count", "public_comment_count", "duration_seconds",
    "source", "collection_status", "missing_metrics", "error", "raw_json",
)


def _row_dict(row: Any) -> dict[str, Any]:
    out = dict(row)
    for key in ("missing_metrics", "raw_json", "related_terms", "components", "weights", "reasons", "trend_sources", "extra_json"):
        if key in out and isinstance(out[key], str):
            try:
                out[key] = json.loads(out[key])
            except ValueError:
                pass
    return out


class PerformanceRepository:
    """video_performance_snapshots / studio_metrics."""

    METRIC_COLUMNS = _PERF_METRIC_COLUMNS

    def __init__(self, db: Database):
        self.db = db

    def upsert_snapshot(self, snapshot: dict[str, Any]) -> None:
        """같은 영상 + 같은 수집일(collected_date)이면 덮어쓴다 (하루 여러 번 수집해도 1행)."""
        data = {c: snapshot.get(c) for c in _PERF_COLUMNS}
        data["missing_metrics"] = json.dumps(snapshot.get("missing_metrics") or [], ensure_ascii=False)
        data["raw_json"] = json.dumps(snapshot.get("raw_json") or {}, ensure_ascii=False, default=str)
        cols = ", ".join(data)
        marks = ", ".join("?" * len(data))
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in ("youtube_video_id", "collected_date"))
        with self.db.session() as conn:
            conn.execute(
                f"INSERT INTO video_performance_snapshots ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(youtube_video_id, collected_date) DO UPDATE SET {updates}",
                tuple(data.values()),
            )

    def snapshots(self, video_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM video_performance_snapshots", []
        if video_id:
            query += " WHERE youtube_video_id = ?"
            params.append(video_id)
        query += " ORDER BY youtube_video_id, collected_at"
        with self.db.session() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_dict(r) for r in rows]

    def latest_by_video(self) -> dict[str, dict[str, Any]]:
        """영상별 가장 최근의 성공(ok/partial) 스냅샷."""
        with self.db.session() as conn:
            rows = conn.execute(
                """SELECT s.* FROM video_performance_snapshots s
                   JOIN (SELECT youtube_video_id, MAX(collected_at) AS last FROM video_performance_snapshots
                         WHERE collection_status IN ('ok', 'partial') GROUP BY youtube_video_id) m
                     ON s.youtube_video_id = m.youtube_video_id AND s.collected_at = m.last"""
            ).fetchall()
        return {r["youtube_video_id"]: _row_dict(r) for r in rows}

    def upsert_studio_metric(self, video_id: str, metric: str, value: float, source: str, imported_at: str,
                             start: str = "", end: str = "") -> None:
        with self.db.session() as conn:
            conn.execute(
                """INSERT INTO studio_metrics (youtube_video_id, metric, value, report_start_date, report_end_date, source, imported_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(youtube_video_id, metric, report_start_date, report_end_date)
                   DO UPDATE SET value = excluded.value, source = excluded.source, imported_at = excluded.imported_at""",
                (video_id, metric, float(value), start, end, source, imported_at),
            )

    def studio_metrics(self, video_id: str | None = None) -> dict[str, dict[str, dict[str, Any]]]:
        """{video_id: {metric: {value, source, report_start_date, report_end_date}}} (같은 지표는 가장 최근 기간)."""
        query, params = "SELECT * FROM studio_metrics", []
        if video_id:
            query += " WHERE youtube_video_id = ?"
            params.append(video_id)
        query += " ORDER BY report_end_date, imported_at"
        out: dict[str, dict[str, dict[str, Any]]] = {}
        with self.db.session() as conn:
            for r in conn.execute(query, params).fetchall():
                out.setdefault(r["youtube_video_id"], {})[r["metric"]] = {
                    "value": r["value"], "source": r["source"],
                    "report_start_date": r["report_start_date"], "report_end_date": r["report_end_date"],
                }
        return out


class TrendRepository:
    """trend_signals 저장 + 같은 날 캐시."""

    def __init__(self, db: Database):
        self.db = db

    def save(self, signal: dict[str, Any]) -> None:
        data = dict(signal)
        data["related_terms"] = json.dumps(data.get("related_terms") or [], ensure_ascii=False)
        data["raw_json"] = json.dumps(data.get("raw_json") or {}, ensure_ascii=False, default=str)
        data.setdefault("region", "")
        cols = ", ".join(data)
        marks = ", ".join("?" * len(data))
        keys = ("provider", "kind", "keyword", "region", "collected_date")
        updates = ", ".join(f"{c} = excluded.{c}" for c in data if c not in keys)
        with self.db.session() as conn:
            conn.execute(
                f"INSERT INTO trend_signals ({cols}) VALUES ({marks}) "
                f"ON CONFLICT(provider, kind, keyword, region, collected_date) DO UPDATE SET {updates}",
                tuple(data.values()),
            )

    def cached(self, provider: str, kind: str, keyword: str, region: str, collected_date: str) -> dict[str, Any] | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM trend_signals WHERE provider=? AND kind=? AND keyword=? AND region=? AND collected_date=?",
                (provider, kind, keyword, region or "", collected_date),
            ).fetchone()
        return _row_dict(row) if row else None

    def recent(self, since_date: str, kind: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM trend_signals WHERE collected_date >= ?", [since_date]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY collected_at DESC"
        with self.db.session() as conn:
            return [_row_dict(r) for r in conn.execute(query, params).fetchall()]


class TopicScoreRepository:
    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def add_many(self, job_id: str | None, rows: Iterable[dict[str, Any]], strategy: str) -> None:
        now = now_iso(self.tz)
        with self.db.session() as conn:
            conn.executemany(
                """INSERT INTO topic_scores (job_id, title, topic, category, selected, duplicate, final_score,
                                             components, weights, reasons, trend_sources, strategy, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        job_id, r["title"], r["topic"], r["category"], int(bool(r.get("selected"))),
                        int(bool(r.get("duplicate"))), r.get("final_score"),
                        json.dumps(r.get("components") or {}, ensure_ascii=False),
                        json.dumps(r.get("weights") or {}, ensure_ascii=False),
                        json.dumps(r.get("reasons") or [], ensure_ascii=False),
                        json.dumps(r.get("trend_sources") or [], ensure_ascii=False),
                        strategy, now,
                    )
                    for r in rows
                ],
            )

    def for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM topic_scores WHERE job_id = ? ORDER BY final_score DESC", (job_id,)).fetchall()
        return [_row_dict(r) for r in rows]


class ProductionMetadataRepository:
    COLUMNS = (
        "youtube_video_id", "topic_category", "topic_score", "trend_sources", "hook_type", "story_structure",
        "loop_ending_enabled", "loop_quality_score", "visual_style", "hook_effect_preset", "video_duration",
        "subtitle_mode", "title_strategy", "script_similarity_score", "originality_score", "extra_json",
    )

    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def upsert(self, job_id: str, **fields: Any) -> None:
        data = {k: fields.get(k) for k in self.COLUMNS}
        data["trend_sources"] = json.dumps(data.get("trend_sources") or [], ensure_ascii=False)
        data["extra_json"] = json.dumps(data.get("extra_json") or {}, ensure_ascii=False, default=str)
        data["loop_ending_enabled"] = int(bool(data.get("loop_ending_enabled")))
        now = now_iso(self.tz)
        cols = ", ".join(["job_id", *data, "created_at", "updated_at"])
        marks = ", ".join("?" * (len(data) + 3))
        updates = ", ".join(f"{c} = excluded.{c}" for c in [*data, "updated_at"])
        with self.db.session() as conn:
            conn.execute(
                f"INSERT INTO production_metadata ({cols}) VALUES ({marks}) ON CONFLICT(job_id) DO UPDATE SET {updates}",
                (job_id, *data.values(), now, now),
            )

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM production_metadata WHERE job_id = ?", (job_id,)).fetchone()
        return _row_dict(row) if row else None

    def all(self) -> list[dict[str, Any]]:
        with self.db.session() as conn:
            return [_row_dict(r) for r in conn.execute("SELECT * FROM production_metadata").fetchall()]


class CommentRepository:
    """video_comments: 영상당 자동 댓글 1행 (등록 상태와 고정 상태를 따로 기록)."""

    COLUMNS = (
        "job_id", "comment_text", "comment_type", "generator", "comment_id", "status", "attempts", "error_code",
        "error_message", "generated_at", "posted_at", "last_attempt_at", "pin_status", "pin_recorded_at", "pin_note",
        "extra_json",
    )

    def __init__(self, db: Database, tz: str = "Asia/Seoul"):
        self.db = db
        self.tz = tz

    def get(self, video_id: str) -> dict[str, Any] | None:
        with self.db.session() as conn:
            row = conn.execute("SELECT * FROM video_comments WHERE youtube_video_id = ?", (video_id,)).fetchone()
        return _row_dict(row) if row else None

    def for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM video_comments WHERE job_id = ? ORDER BY id DESC", (job_id,)).fetchall()
        return [_row_dict(r) for r in rows]

    def upsert(self, video_id: str, **fields: Any) -> None:
        data = {k: v for k, v in fields.items() if k in self.COLUMNS}
        if "extra_json" in data:
            data["extra_json"] = json.dumps(data["extra_json"] or {}, ensure_ascii=False, default=str)
        now = now_iso(self.tz)
        cols = ["youtube_video_id", *data, "created_at", "updated_at"]
        updates = ", ".join(f"{c} = excluded.{c}" for c in [*data, "updated_at"])
        with self.db.session() as conn:
            conn.execute(
                f"INSERT INTO video_comments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                f"ON CONFLICT(youtube_video_id) DO UPDATE SET {updates}",
                (video_id, *data.values(), now, now),
            )

    def list(self, statuses: Iterable[str] | None = None, pin_statuses: Iterable[str] | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        query, params, where = "SELECT * FROM video_comments", [], []
        for column, values in (("status", list(statuses or [])), ("pin_status", list(pin_statuses or []))):
            if values:
                where.append(f"{column} IN ({','.join('?' * len(values))})")
                params += values
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at DESC, id DESC LIMIT ?"
        with self.db.session() as conn:
            return [_row_dict(r) for r in conn.execute(query, (*params, limit)).fetchall()]

    def texts(self, exclude_video: str | None = None, limit: int = 30) -> list[str]:
        """이전 자동 댓글 본문 (반복 댓글 방지용)."""
        with self.db.session() as conn:
            rows = conn.execute(
                "SELECT comment_text FROM video_comments WHERE comment_text != '' AND youtube_video_id != ? "
                "ORDER BY id DESC LIMIT ?", (exclude_video or "", limit),
            ).fetchall()
        return [r["comment_text"] for r in rows]


def uploaded_jobs(db: Database, since_iso: str | None = None) -> list[JobRecord]:
    """YouTube 에 올라간 작업 (성과 수집 대상)."""
    query = "SELECT * FROM jobs WHERE youtube_video_id IS NOT NULL AND youtube_video_id != ''"
    params: list[Any] = []
    if since_iso:
        query += " AND COALESCE(uploaded_at, created_at) >= ?"
        params.append(since_iso)
    query += " ORDER BY created_at DESC"
    with db.session() as conn:
        return [JobRecord.from_row(r) for r in conn.execute(query, params).fetchall()]
