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
