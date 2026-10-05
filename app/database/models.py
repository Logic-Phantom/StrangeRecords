"""DB 레코드 모델."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any


class TopicStatus:
    SELECTED = "selected"      # 오늘 주제로 선정 (중복 검사 대상)
    PRODUCED = "produced"      # 영상 제작 완료 (업로드 전)
    UPLOADED = "uploaded"      # YouTube 업로드 완료
    FAILED = "failed"          # 제작 실패 (retry 대상, 중복 검사 대상)
    DISCARDED = "discarded"    # 폐기 (중복 검사에서 제외)


class JobStatus:
    RUNNING = "running"
    FAILED = "failed"
    RENDERED = "rendered"      # 품질 검사 통과, 업로드 대기
    SUCCESS = "success"        # 업로드(또는 test 모드 제작) 완료


@dataclass
class TopicRecord:
    id: int
    title: str
    category: str
    topic: str
    keywords: list[str]
    summary: str
    truth_status: str
    created_at: str
    status: str
    job_id: str | None = None
    video_id: str | None = None
    youtube_url: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "TopicRecord":
        return cls(
            id=row["id"],
            title=row["title"],
            category=row["category"],
            topic=row["topic"],
            keywords=json.loads(row["keywords"] or "[]"),
            summary=row["summary"],
            truth_status=row["truth_status"],
            created_at=row["created_at"],
            status=row["status"],
            job_id=row["job_id"],
            video_id=row["video_id"],
            youtube_url=row["youtube_url"],
        )


@dataclass
class JobRecord:
    id: str
    mode: str
    upload_enabled: bool
    status: str
    created_at: str
    updated_at: str
    topic_id: int | None = None
    current_step: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    retry_count: int = 0
    video_path: str | None = None
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    publish_at: str | None = None
    uploaded_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "JobRecord":
        return cls(
            id=row["id"],
            mode=row["mode"],
            upload_enabled=bool(row["upload_enabled"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            topic_id=row["topic_id"],
            current_step=row["current_step"],
            completed_steps=json.loads(row["completed_steps"] or "[]"),
            state=json.loads(row["state_json"] or "{}"),
            error=row["error"],
            retry_count=row["retry_count"],
            video_path=row["video_path"],
            youtube_video_id=row["youtube_video_id"],
            youtube_url=row["youtube_url"],
            publish_at=row["publish_at"],
            uploaded_at=row["uploaded_at"],
        )
