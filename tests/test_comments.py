"""자동 댓글: 생성 검수 / 공개 상태별 처리 / 권한 부족 / 타임아웃 후 중복 방지 / 업로드 격리 / 고정 상태 분리 (실제 API 호출 없음)."""

from __future__ import annotations

import json

import pytest

from app.ai.comment_generator import CommentGenerator, fallback_comment, validate_comment
from app.database.models import JobStatus
from app.database.repositories import CommentRepository, JobRepository
from app.schemas import Explainer, Hypothesis
from app.youtube.comments import COMMENT_SCOPE, CommentService, CommentStatus, PinStatus, write_pin_guide

TITLE = "1872년 메리 셀레스트호, 사람만 사라진 배"
SCRIPT = ("1872년 12월, 대서양 한가운데서 사람이 한 명도 없는 배가 발견됐습니다. 배의 이름은 메리 셀레스트호였죠. "
          "식량과 물은 여섯 달 치가 그대로 남아 있었습니다. 사라진 것은 구명보트 하나뿐이었습니다. "
          "알코올 증기를 두려워해 잠시 피했다는 가설과 선상 반란 가설이 있습니다. 여러분은 그들이 왜 배를 떠났다고 생각하나요?")
GOOD = ("이번 이야기에서 가장 설명하기 어려웠던 단서는 그대로 남은 여섯 달 치 식량과 사라진 구명보트였습니다. "
        "알코올 증기 가설과 선상 반란 가설 중 어느 쪽이 더 설득력 있다고 보시나요? 그렇게 판단한 근거도 궁금합니다.")
EXPLAINER = Explainer(key_clues=["사라진 구명보트"],
                      hypotheses=[Hypothesis(name="알코올 증기 가설"), Hypothesis(name="선상 반란 가설")])


def state(video_id="vid123"):
    return {
        "metadata": {"title": TITLE},
        "content": {"script": SCRIPT, "comment_prompt": "여러분은 그들이 왜 배를 떠났다고 생각하나요?"},
        "draft": {"explainer": EXPLAINER.model_dump()},
        "upload": {"video_id": video_id},
    }


# --------------------------------------------------------------- 검수
def test_good_comment_passes(settings):
    assert validate_comment(GOOD, SCRIPT, TITLE, settings) == []


@pytest.mark.parametrize("text, reason", [
    ("", "비어"),
    ("이번 [핵심 단서]에 대해 [가설 A]와 [가설 B] 중 무엇이 맞을까요? 메리 셀레스트호 식량", "미완성"),
    ("메리 셀레스트호 식량과 구명보트 이야기 어떠셨나요? 구독과 좋아요 부탁드립니다!", "홍보"),
    ("메리 셀레스트호 구명보트 이야기는 다음 편에서 더 자세히 다룹니다. 기대되시나요?", "후속편"),
    ("제가 직접 가 봤는데 메리 셀레스트호 구명보트는 정말 작더군요. 여러분은 어떻게 생각하세요?", "허위 경험"),
    ("오늘 저녁 메뉴는 김치찌개와 계란말이였습니다. 여러분은 어떤 음식을 좋아하시나요?", "관련된 핵심어"),
    ("메리 셀레스트호 구명보트는 1901년에 다시 발견됐다는데 어떻게 생각하세요? 식량 이야기도 궁금합니다.", "연도"),
    ("메리 셀레스트호 구명보트 정보는 https://example.com 에 있습니다. 식량 이야기는 어떠셨나요?", "링크"),
])
def test_bad_comments_are_rejected(settings, text, reason):
    problems = validate_comment(text, SCRIPT, TITLE, settings)
    assert problems and any(reason in p for p in problems), problems


def test_repeated_comment_is_rejected(settings):
    assert any("반복" in p for p in validate_comment(GOOD, SCRIPT, TITLE, settings, previous_texts=[GOOD]))


def test_fallback_uses_video_content(settings):
    text, kind = fallback_comment(TITLE, "여러분은 그들이 왜 배를 떠났다고 생각하나요?", EXPLAINER)
    assert kind == "hypothesis_compare" and "알코올 증기 가설" in text and text.endswith("궁금합니다.")
    assert validate_comment(text, SCRIPT, TITLE, settings) == []


class FakeGemini:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def generate_json(self, prompt, model, validate=None, **kw):
        # GeminiClient.generate_json 처럼: 검수 실패면 다음 응답으로 재요청
        for text in self.responses:
            self.calls += 1
            result = model.model_validate(json.loads(text))
            try:
                validate(result)
                return result
            except Exception:
                continue
        raise RuntimeError("all failed")


def test_generator_regenerates_until_valid(settings):
    bad = json.dumps({"comment_type": "clue_question", "text": "구독 부탁드립니다! 메리 셀레스트호 어떠셨나요?"})
    good = json.dumps({"comment_type": "hypothesis_compare", "text": GOOD})
    draft = CommentGenerator(settings, FakeGemini([bad, good])).generate(TITLE, SCRIPT, explainer=EXPLAINER)
    assert draft.ok and draft.generator == "gemini" and draft.text == GOOD


# ------------------------------------------------------------- fake API
class Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class HttpErr(Exception):
    def __init__(self, status, reason):
        super().__init__(f"HTTP {status} {reason}")
        self.resp = type("R", (), {"status": status})()
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class FakeYouTube:
    def __init__(self, privacy="public", publish_at=None, insert_error=None, existing=None):
        self.privacy, self.publish_at = privacy, publish_at
        self.insert_error = insert_error
        self.existing = existing or []      # 이미 달린 댓글 [(author, text, id)]
        self.inserted: list[dict] = []

    def videos(self):
        fake = self

        class V:
            def list(self, **kw):
                return Req(lambda: {"items": [{"status": {"privacyStatus": fake.privacy, "publishAt": fake.publish_at},
                                               "snippet": {"channelId": "UCme"}}]})
        return V()

    def commentThreads(self):
        fake = self

        class C:
            def list(self, **kw):
                return Req(lambda: {"items": [
                    {"id": cid, "snippet": {"topLevelComment": {"id": cid, "snippet": {
                        "authorChannelId": {"value": author}, "textOriginal": text}}}}
                    for author, text, cid in fake.existing]})

            def insert(self, part, body):
                def run():
                    if fake.insert_error:
                        err, fake.insert_error = fake.insert_error, None
                        if isinstance(err, Exception) and getattr(err, "posts_anyway", False):
                            fake.existing.append(("UCme", body["snippet"]["topLevelComment"]["snippet"]["textOriginal"], "c-late"))
                        raise err
                    fake.inserted.append(body)
                    return {"id": "t1", "snippet": {"topLevelComment": {"id": f"c{len(fake.inserted)}"}}}
                return Req(run)
        return C()


def make_service(settings, db, yt, scopes=(COMMENT_SCOPE,)):
    settings.comments.enabled = True
    settings.comments.use_gemini = False
    return CommentService(settings, db, service_factory=lambda: yt, scopes=lambda: list(scopes))


def make_job(db, settings, video_id="vid123"):
    jobs = JobRepository(db, settings.app.timezone)
    jobs.create("job1", "production", True)
    jobs.update("job1", status=JobStatus.SUCCESS, youtube_video_id=video_id, state_json=json.dumps(state(video_id)),
                uploaded_at=__import__("app.utils.files", fromlist=["now_iso"]).now_iso(settings.app.timezone))
    return jobs.get("job1")


# ---------------------------------------------------------------- 등록
def test_public_video_gets_one_top_level_comment(settings, db):
    yt = FakeYouTube()
    service = make_service(settings, db, yt)
    job = make_job(db, settings)
    res = service.process_job(job)
    assert res["status"] == CommentStatus.POSTED
    body = yt.inserted[0]["snippet"]
    assert body["videoId"] == "vid123" and body["channelId"] == "UCme"
    assert "알코올 증기 가설과 선상 반란 가설" in body["topLevelComment"]["snippet"]["textOriginal"]  # 영상 속 가설
    # 다시 실행해도 같은 영상에 두 번 등록하지 않는다
    assert service.process_job(job)["status"] == CommentStatus.POSTED and len(yt.inserted) == 1
    row = CommentRepository(db).get("vid123")
    # 등록 성공 ≠ 고정 성공
    assert row["status"] == "posted" and row["pin_status"] == PinStatus.MANUAL_REQUIRED


@pytest.mark.parametrize("privacy, publish_at", [("private", "2026-10-17T03:00:00Z"), ("private", None), ("unlisted", None)])
def test_private_or_scheduled_video_waits(settings, db, privacy, publish_at):
    yt = FakeYouTube(privacy=privacy, publish_at=publish_at)
    res = make_service(settings, db, yt).process_job(make_job(db, settings))
    assert res["status"] == CommentStatus.WAITING and not yt.inserted
    # 공개되면 retry 로 등록 (본문은 처음 만든 것을 재사용)
    text = CommentRepository(db).get("vid123")["comment_text"]
    yt.privacy = "public"
    results = make_service(settings, db, yt).retry_all()
    assert results[0][1]["status"] == CommentStatus.POSTED
    assert yt.inserted[0]["snippet"]["topLevelComment"]["snippet"]["textOriginal"] == text


def test_missing_scope_does_not_call_api(settings, db):
    yt = FakeYouTube()
    res = make_service(settings, db, yt, scopes=()).process_job(make_job(db, settings))
    row = CommentRepository(db).get("vid123")
    assert res["status"] == CommentStatus.PERMISSION and not yt.inserted and row["attempts"] == 0


def test_permission_error_from_api(settings, db):
    yt = FakeYouTube(insert_error=HttpErr(403, "insufficientPermissions"))
    res = make_service(settings, db, yt).process_job(make_job(db, settings))
    assert res["status"] == CommentStatus.PERMISSION and "403" in res["error_code"]


def test_comments_disabled_on_video_is_not_retried(settings, db):
    yt = FakeYouTube(insert_error=HttpErr(403, "commentsDisabled"))
    service = make_service(settings, db, yt)
    assert service.process_job(make_job(db, settings))["status"] == CommentStatus.REJECTED
    assert service.retry_all() == []


def test_timeout_then_retry_finds_existing_comment(settings, db):
    err = TimeoutError("read timed out")
    err.posts_anyway = True   # 요청은 실패로 보였지만 실제로는 등록됨
    yt = FakeYouTube(insert_error=err)
    service = make_service(settings, db, yt)
    job = make_job(db, settings)
    assert service.process_job(job)["status"] == CommentStatus.FAILED
    res = make_service(settings, db, yt).retry_all()[0][1]
    assert res["status"] == CommentStatus.POSTED and res["comment_id"] == "c-late"
    assert yt.inserted == []   # 새로 insert 하지 않음 → 중복 댓글 없음


def test_max_attempts_stops_retrying(settings, db):
    settings.comments.max_attempts = 2
    yt = FakeYouTube()
    service = make_service(settings, db, yt)
    job = make_job(db, settings)
    for _ in range(2):
        yt.insert_error = HttpErr(503, "backendError")
        assert service.process_job(job)["status"] == CommentStatus.FAILED
    assert service.process_job(job)["status"] == CommentStatus.SKIPPED and not yt.inserted


def test_invalid_comment_is_not_posted(settings, db):
    yt = FakeYouTube()
    service = make_service(settings, db, yt)
    job = make_job(db, settings)
    job.state["content"]["comment_prompt"] = ""
    job.state["draft"]["explainer"] = {}
    job.state["content"]["script"] = "짧은 대본입니다."
    res = service.process_job(job)
    assert res["status"] == CommentStatus.REJECTED and res["error_code"] == "comment_validation" and not yt.inserted


def test_disabled_does_nothing(settings, db):
    yt = FakeYouTube()
    service = make_service(settings, db, yt)
    settings.comments.enabled = False
    assert service.process_job(make_job(db, settings))["status"] == "disabled"
    assert CommentRepository(db).get("vid123") is None and not yt.inserted


# ------------------------------------------------------------------ 고정
def test_pin_is_manual_and_recorded_separately(settings, db, tmp_path):
    yt = FakeYouTube()
    service = make_service(settings, db, yt)
    job = make_job(db, settings)
    ok, msg = service.mark_pinned("vid123")
    assert not ok and "기록 없음" in msg                     # 등록 전에는 고정 기록 불가
    service.process_job(job)
    guide = write_pin_guide(settings, db, tmp_path / "pin.md").read_text(encoding="utf-8")
    assert "vid123" in guide and "고정" in guide and "mark-pinned" in guide
    ok, msg = service.mark_pinned("job1", note="Studio")
    row = CommentRepository(db).get("vid123")
    assert ok and row["pin_status"] == PinStatus.PINNED_MANUAL and "API 확인 불가" in row["pin_note"]
    assert row["status"] == CommentStatus.POSTED


# --------------------------------------------------------- 파이프라인 격리
def test_comment_failure_does_not_fail_upload(settings, db, monkeypatch):
    from app.pipeline.daily_pipeline import STEPS, DailyPipeline, JobContext, RunOptions

    assert STEPS.index("upload") < STEPS.index("comment") < STEPS.index("record")
    settings.comments.enabled = True
    settings.comments.use_gemini = False
    pipeline = DailyPipeline(settings, db=db)
    pipeline.options = RunOptions(upload=True)
    job = make_job(db, settings)

    def boom(self):
        raise RuntimeError("network down")

    monkeypatch.setattr(CommentService, "service", property(boom))
    monkeypatch.setattr("app.youtube.comments.granted_scopes", lambda s: [COMMENT_SCOPE])
    ctx = JobContext(job=job, state=job.state)
    pipeline.step_comment(ctx)          # 예외를 올리지 않는다
    assert ctx.state["comment"]["status"] == CommentStatus.FAILED
    assert pipeline.jobs.get("job1").status == JobStatus.SUCCESS


def test_resume_does_not_reupload_uploaded_job(settings, db, monkeypatch):
    from app.pipeline.daily_pipeline import DailyPipeline

    pipeline = DailyPipeline(settings, db=db)
    job = make_job(db, settings)
    pipeline.jobs.update("job1", status=JobStatus.FAILED,
                         completed_steps=json.dumps(["topic", "upload"]))
    ran: list[str] = []
    monkeypatch.setattr(DailyPipeline, "_run", lambda self, ctx: ran.extend(ctx.completed) or ctx)
    pipeline.resume(job.id, upload=True)
    assert "upload" in ran   # 이미 올라간 영상(state.upload.video_id)은 다시 업로드하지 않음
