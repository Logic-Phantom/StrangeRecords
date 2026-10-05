"""설정 / JSON 파싱 / Retry / DB 단위 테스트."""

from __future__ import annotations

import pytest

from app.ai.json_parser import JSONParseError, parse_json
from app.database.models import JobStatus, TopicStatus
from app.database.repositories import JobRepository, TopicRepository
from app.schemas import TopicCandidate
from app.utils.retry import NonRetryableError, RetryError, retry_call


def test_settings_loaded(settings):
    assert settings.video.width == 1080 and settings.video.height == 1920
    assert settings.video.fps == 30
    assert settings.gemini.model  # 모델명은 config 한 곳에서 관리
    assert len(settings.categories) == 5
    assert settings.script.min_chars < settings.script.max_chars


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Here you go:\n{"a": 1}\nThanks',
        '{"a": 1,}',
    ],
)
def test_parse_json_variants(text):
    assert parse_json(text) == {"a": 1}


def test_parse_json_failure():
    with pytest.raises(JSONParseError):
        parse_json("no json here")


def test_retry_succeeds_after_failures():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary")
        return "ok"

    assert retry_call(flaky, step="t", max_attempts=3, delays=((0, 0),), sleep=lambda s: None) == "ok"
    assert calls["n"] == 3


def test_retry_is_bounded():
    calls = {"n": 0}

    def always_fail():
        calls["n"] += 1
        raise RuntimeError("down")

    with pytest.raises(RetryError):
        retry_call(always_fail, step="t", max_attempts=3, delays=((0, 0),), sleep=lambda s: None)
    assert calls["n"] == 3  # 무한 재시도 금지


def test_non_retryable_stops_immediately():
    calls = {"n": 0}

    def bad_key():
        calls["n"] += 1
        raise NonRetryableError("invalid key")

    with pytest.raises(NonRetryableError):
        retry_call(bad_key, step="t", max_attempts=3, sleep=lambda s: None)
    assert calls["n"] == 1


def test_topic_and_job_repositories(db):
    topics, jobs = TopicRepository(db), JobRepository(db)
    tid = topics.add(TopicCandidate(title="크라켄", topic="크라켄 전설", category="legendary_creatures", keywords=["크라켄"]))
    topics.update(tid, status=TopicStatus.UPLOADED, youtube_url="https://youtube.com/shorts/x")
    history = topics.history()
    assert history[0].keywords == ["크라켄"] and history[0].status == TopicStatus.UPLOADED
    assert "legendary_creatures" in topics.last_used_by_category()

    job = jobs.create("20260101-100000", "production", True)
    jobs.save_state(job.id, ["topic"], {"topic": {"title": "x"}}, "research")
    loaded = jobs.get(job.id)
    assert loaded.completed_steps == ["topic"] and loaded.state["topic"]["title"] == "x"
    jobs.update(job.id, status=JobStatus.FAILED)
    assert jobs.latest([JobStatus.FAILED]).id == job.id
