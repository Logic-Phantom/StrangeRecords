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
"""


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
