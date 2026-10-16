"""영상 공개 후 최상위 댓글 자동 등록 + 고정(pin) 상태의 분리 기록.

공식 문서 확인 (2026-10-16)
- commentThreads.insert: 최상위 댓글 생성, 범위 youtube.force-ssl 필요, 50 units,
  필수 snippet.channelId / snippet.videoId / snippet.topLevelComment.snippet.textOriginal
  https://developers.google.com/youtube/v3/docs/commentThreads/insert
- 댓글 고정: comments / commentThreads 리소스에 고정 속성·메서드가 없음 → API 로 고정할 수 없다.
  https://developers.google.com/youtube/v3/docs/comments , https://support.google.com/youtube/answer/6000964
  → 등록 성공을 고정 성공으로 기록하지 않고 pin_status=manual_required 로 둔 뒤, 사람이 고정하면 `comments mark-pinned` 로 기록한다.
  브라우저 자동화·비공개 엔드포인트로 고정하지 않는다.

중복 방지
- video_comments 는 영상당 1행(UNIQUE). 이미 posted 면 다시 등록하지 않는다.
- 본문은 처음 한 번만 생성해 저장하고 재시도 때 같은 본문을 쓴다 (오류마다 새 댓글을 만들지 않음).
- 등록 전에 항상 그 영상의 최근 댓글에서 채널 계정이 단 같은 본문을 찾는다 → 타임아웃으로 실제로는 등록된 경우 posted 로 정리.
실패 격리: 이 모듈의 오류는 영상 제작/업로드 상태를 바꾸지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.ai.comment_generator import CommentGenerator
from app.config.settings import Settings
from app.database.db import Database
from app.database.models import JobRecord
from app.database.repositories import CommentRepository, JobRepository
from app.schemas import Explainer, ResearchNotes
from app.topics.duplicate_checker import text_similarity
from app.utils.files import now_iso
from app.utils.logger import get_logger
from app.youtube.auth import COMMENT_SCOPES

logger = get_logger("youtube.comments")

COMMENT_SCOPE = COMMENT_SCOPES[0]
REAUTH_GUIDE = (
    "댓글 등록 권한(youtube.force-ssl)이 token.json 에 없습니다. 업로드는 지금 토큰으로 계속 동작합니다.\n"
    "  ① Google 인증 플랫폼 → 데이터 액세스에 .../auth/youtube.force-ssl 범위 추가 → ② 로컬에서 `python -m app.main auth`"
    " → ③ 새 token.json 내용으로 GitHub Secret YOUTUBE_TOKEN_JSON 갱신 → ④ `python -m app.main comments retry`"
)


class CommentStatus:
    PENDING = "pending"                    # 본문 생성 완료, 아직 등록 전
    WAITING = "waiting_publish"            # 비공개/예약 영상 → 공개될 때까지 대기
    POSTED = "posted"                      # commentThreads.insert 성공 (고정 여부와 무관)
    FAILED = "failed"                      # 일시 오류 (타임아웃/5xx/429) → 재시도 대상
    PERMISSION = "permission_denied"       # 권한 부족 → 재인증 후 재시도
    REJECTED = "rejected"                  # 댓글 검수 실패 / 댓글 사용 중지 / 영상 없음 등 (자동 재시도 안 함)
    SKIPPED = "skipped"                    # 대기 기간 만료, 시도 횟수 초과
    MISSING = "missing"                    # 등록했지만 지금은 조회되지 않음 (삭제/보류 등)


class PinStatus:
    NOT_APPLICABLE = "not_applicable"      # 아직 등록 전
    MANUAL_REQUIRED = "manual_required"    # API 미지원 → 사람이 Studio 에서 고정해야 함
    PINNED_MANUAL = "pinned_manual"        # 사람이 고정했다고 기록 (API 로 확인한 값 아님)
    UNKNOWN = "unknown"                    # 확인할 수 없음


RETRYABLE = (CommentStatus.PENDING, CommentStatus.WAITING, CommentStatus.FAILED, CommentStatus.PERMISSION)


@dataclass
class VideoState:
    video_id: str
    exists: bool
    privacy_status: str = ""
    publish_at: str | None = None
    channel_id: str = ""
    upload_status: str = ""


def _http_reason(exc: Exception) -> str:
    content = getattr(exc, "content", b"") or b""
    try:
        data = json.loads(content.decode("utf-8") if isinstance(content, bytes) else content)
        errors = (data.get("error") or {}).get("errors") or []
        if errors:
            return str(errors[0].get("reason") or "")
        return str((data.get("error") or {}).get("status") or "")
    except (ValueError, AttributeError):
        return ""


def classify_error(exc: Exception) -> tuple[str, str]:
    """(상태, 오류 코드). 토큰 값이 섞이지 않도록 오류 코드/사유만 기록한다."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is not None:
        status = int(status)
        reason = _http_reason(exc)
        code = f"http_{status}" + (f"_{reason}" if reason else "")
        if status in (401, 403) and reason in ("insufficientPermissions", "forbidden", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                                                "PERMISSION_DENIED", "authError", ""):
            return CommentStatus.PERMISSION, code
        if status == 429 or status >= 500:
            return CommentStatus.FAILED, code
        return CommentStatus.REJECTED, code      # 400 텍스트 오류, 403 commentsDisabled/ineligibleAccount, 404 등
    name = type(exc).__name__
    if name in ("RefreshError", "YouTubeAuthError"):
        return CommentStatus.PERMISSION, name
    return CommentStatus.FAILED, name             # 타임아웃·연결 오류: 실제로 등록됐을 수 있음 → 다음 시도 전에 조회


def get_video_state(service, video_id: str) -> VideoState:
    items = service.videos().list(part="status,snippet", id=video_id).execute().get("items", [])
    if not items:
        return VideoState(video_id, exists=False)
    item = items[0]
    status, snippet = item.get("status", {}), item.get("snippet", {})
    return VideoState(video_id, True, status.get("privacyStatus", ""), status.get("publishAt"),
                      snippet.get("channelId", ""), status.get("uploadStatus", ""))


def find_existing_comment(service, video_id: str, channel_id: str, text: str, threshold: float = 0.9) -> str | None:
    """그 영상의 최근 최상위 댓글 중 채널 계정이 단 같은 본문이 있으면 그 댓글 ID (타임아웃 후 중복 등록 방지)."""
    resp = service.commentThreads().list(part="snippet", videoId=video_id, maxResults=100, order="time",
                                         textFormat="plainText").execute()
    for item in resp.get("items", []):
        top = (item.get("snippet") or {}).get("topLevelComment") or {}
        snip = top.get("snippet") or {}
        author = (snip.get("authorChannelId") or {}).get("value", "")
        if channel_id and author and author != channel_id:
            continue
        body = snip.get("textOriginal") or snip.get("textDisplay") or ""
        if body.strip() == text.strip() or text_similarity(body, text) >= threshold:
            return top.get("id") or item.get("id")
    return None


def insert_comment(service, video_id: str, channel_id: str, text: str) -> str:
    body = {"snippet": {"channelId": channel_id, "videoId": video_id,
                        "topLevelComment": {"snippet": {"textOriginal": text}}}}
    resp = service.commentThreads().insert(part="snippet", body=body).execute()
    return ((resp.get("snippet") or {}).get("topLevelComment") or {}).get("id") or resp.get("id", "")


def granted_scopes(settings: Settings) -> list[str]:
    from app.analytics.youtube_analytics import token_scopes

    return token_scopes(settings)


class CommentService:
    def __init__(self, settings: Settings, db: Database, service_factory: Callable[[], Any] | None = None,
                 gemini=None, scopes: Callable[[], list[str]] | None = None):
        self.settings = settings
        self.cfg = settings.comments
        self.tz = settings.app.timezone
        self.repo = CommentRepository(db, self.tz)
        self.jobs = JobRepository(db, self.tz)
        self._factory = service_factory
        self._service = None
        self._gemini = gemini
        self._scopes = scopes or (lambda: granted_scopes(settings))

    @property
    def service(self):
        if self._service is None:
            if self._factory:
                self._service = self._factory()
            else:
                from app.youtube.auth import build_service

                self._service = build_service(self.settings, interactive=False)
        return self._service

    # -------------------------------------------------------------- text
    def _job_text(self, state: dict) -> dict:
        meta, content, draft = state.get("metadata") or {}, state.get("content") or {}, state.get("draft") or {}
        research = ResearchNotes(**state["research"]) if state.get("research") else None
        return {
            "title": meta.get("title") or content.get("title") or draft.get("title", ""),
            "script": content.get("script") or draft.get("script", ""),
            "comment_prompt": content.get("comment_prompt") or draft.get("comment_prompt", ""),
            "research": research,
            "explainer": Explainer(**(draft.get("explainer") or {})),
        }

    def generate_text(self, state: dict, exclude_video: str | None = None):
        info = self._job_text(state)
        previous = self.repo.texts(exclude_video=exclude_video)
        return CommentGenerator(self.settings, self._gemini).generate(
            info["title"], info["script"], info["comment_prompt"], info["research"], info["explainer"], previous)

    # ----------------------------------------------------------- process
    def _expired(self, job: JobRecord) -> bool:
        uploaded = job.uploaded_at or job.created_at
        try:
            when = datetime.fromisoformat(uploaded)
        except (TypeError, ValueError):
            return False
        now = datetime.now(ZoneInfo(self.tz))
        if when.tzinfo is None:
            when = when.replace(tzinfo=ZoneInfo(self.tz))
        return now - when > timedelta(days=self.cfg.retry_window_days)

    def process_job(self, job: JobRecord, regenerate: bool = False, force: bool = False) -> dict:
        """작업 하나의 댓글 단계. 어떤 경우에도 예외를 올리지 않고 결과 dict 를 돌려준다."""
        video_id = job.youtube_video_id or (job.state.get("upload") or {}).get("video_id")
        if not self.cfg.enabled and not force:
            return {"status": "disabled", "message": "comments.enabled=false"}
        if not video_id:
            return {"status": "no_video", "message": "업로드된 영상 ID 없음"}
        try:
            return self._process(job, video_id, regenerate)
        except Exception as exc:  # 마지막 안전망: 댓글 단계 실패가 업로드 결과를 바꾸지 않게
            status, code = classify_error(exc)
            logger.warning("댓글 단계 오류 (%s): %s", code, str(exc).splitlines()[0][:200])
            self.repo.upsert(video_id, job_id=job.id, status=status, error_code=code,
                             error_message=str(exc)[:500], last_attempt_at=now_iso(self.tz))
            return {"status": status, "error_code": code}

    def _process(self, job: JobRecord, video_id: str, regenerate: bool) -> dict:
        record = self.repo.get(video_id) or {}
        status = record.get("status")
        if status == CommentStatus.POSTED:
            return {"status": status, "comment_id": record.get("comment_id"), "message": "이미 등록됨 (중복 등록 안 함)"}
        if status in (CommentStatus.SKIPPED, CommentStatus.MISSING) or (
                status == CommentStatus.REJECTED and not (regenerate and record.get("error_code") == "comment_validation")):
            return {"status": status, "message": record.get("error_message") or "자동 재시도 대상 아님"}
        attempts = int(record.get("attempts") or 0)
        if attempts >= self.cfg.max_attempts:
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.SKIPPED, error_code="max_attempts",
                             error_message=f"최대 시도 {self.cfg.max_attempts}회 초과")
            return {"status": CommentStatus.SKIPPED, "message": "최대 시도 횟수 초과"}
        if self._expired(job) and status != CommentStatus.PERMISSION:
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.SKIPPED, error_code="window_expired",
                             error_message=f"업로드 후 {self.cfg.retry_window_days}일이 지나 자동 등록 안 함")
            return {"status": CommentStatus.SKIPPED, "message": "대기 기간 만료"}

        # 1) 본문: 처음 한 번만 생성해 저장 (재시도는 같은 본문)
        text = record.get("comment_text") or ""
        if not text or regenerate:
            draft = self.generate_text(job.state, exclude_video=video_id)
            fields = dict(job_id=job.id, comment_text=draft.text, comment_type=draft.comment_type,
                          generator=draft.generator, generated_at=now_iso(self.tz), pin_status=PinStatus.NOT_APPLICABLE)
            if not draft.ok:
                self.repo.upsert(video_id, **fields, status=CommentStatus.REJECTED, error_code="comment_validation",
                                 error_message="; ".join(draft.problems)[:500])
                logger.warning("자동 댓글 검수 실패 → 등록 안 함: %s", "; ".join(draft.problems))
                return {"status": CommentStatus.REJECTED, "error_code": "comment_validation", "problems": draft.problems}
            self.repo.upsert(video_id, **fields, status=CommentStatus.PENDING, error_code=None, error_message=None)
            text = draft.text

        # 2) 권한: 없으면 API 를 호출하지 않는다 (시도 횟수도 늘리지 않음)
        if COMMENT_SCOPE not in self._scopes():
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.PERMISSION, error_code="insufficient_scope",
                             error_message="token.json 에 youtube.force-ssl 범위 없음")
            logger.warning(REAUTH_GUIDE)
            return {"status": CommentStatus.PERMISSION, "error_code": "insufficient_scope"}

        # 3) 영상 상태: 실제로 공개된 영상에만 등록
        state = get_video_state(self.service, video_id)
        if not state.exists:
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.REJECTED, error_code="video_not_found",
                             error_message="YouTube 에서 영상을 찾을 수 없음 (삭제됨?)", last_attempt_at=now_iso(self.tz))
            return {"status": CommentStatus.REJECTED, "error_code": "video_not_found"}
        allowed = {"public"} | ({"unlisted"} if self.cfg.post_when == "public_or_unlisted" else set())
        if state.privacy_status not in allowed:
            reason = f"예약 공개 대기 (publishAt={state.publish_at})" if state.publish_at else f"현재 {state.privacy_status or '?'} 상태"
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.WAITING, error_code=f"privacy_{state.privacy_status}",
                             error_message=reason)
            logger.info("댓글 대기: %s → 공개되면 `comments retry` 로 등록", reason)
            return {"status": CommentStatus.WAITING, "message": reason}

        # 4) 이미 달려 있는지 먼저 확인 (이전 시도가 타임아웃이었지만 실제로는 등록된 경우)
        now = now_iso(self.tz)
        existing = find_existing_comment(self.service, video_id, state.channel_id, text)
        if existing:
            self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.POSTED, comment_id=existing,
                             posted_at=record.get("posted_at") or now, error_code=None, error_message=None,
                             pin_status=PinStatus.MANUAL_REQUIRED, last_attempt_at=now)
            logger.info("같은 자동 댓글이 이미 등록되어 있음 → 새로 등록하지 않음 (%s)", existing)
            return {"status": CommentStatus.POSTED, "comment_id": existing, "message": "기존 댓글 확인"}

        # 5) 등록
        try:
            comment_id = insert_comment(self.service, video_id, state.channel_id, text)
        except Exception as exc:
            status, code = classify_error(exc)
            self.repo.upsert(video_id, job_id=job.id, status=status, attempts=attempts + 1, error_code=code,
                             error_message=str(exc).splitlines()[0][:500], last_attempt_at=now)
            if status == CommentStatus.PERMISSION:
                logger.warning("댓글 등록 권한 오류 (%s)\n%s", code, REAUTH_GUIDE)
            else:
                logger.warning("댓글 등록 실패 (%s, %d/%d)", code, attempts + 1, self.cfg.max_attempts)
            return {"status": status, "error_code": code}
        self.repo.upsert(video_id, job_id=job.id, status=CommentStatus.POSTED, comment_id=comment_id, posted_at=now,
                         attempts=attempts + 1, error_code=None, error_message=None, last_attempt_at=now,
                         pin_status=PinStatus.MANUAL_REQUIRED)
        logger.info("자동 댓글 등록 완료 (comment=%s). 고정은 API 미지원 → Studio 에서 직접 고정 후 `comments mark-pinned %s`",
                    comment_id, video_id)
        return {"status": CommentStatus.POSTED, "comment_id": comment_id}

    # ------------------------------------------------------------ batch
    def retry_all(self, job_id: str | None = None, regenerate: bool = False) -> list[tuple[str, dict]]:
        """재시도 대상: 대기/실패/권한 부족 행 + 아직 댓글 기록이 없는 최근 업로드 작업."""
        from app.database.repositories import uploaded_jobs

        jobs = [self.jobs.get(job_id)] if job_id else uploaded_jobs(self.jobs.db)
        out: list[tuple[str, dict]] = []
        for job in jobs:
            if not job or not job.youtube_video_id:
                continue
            record = self.repo.get(job.youtube_video_id)
            if record and record["status"] not in RETRYABLE and not (job_id and regenerate):
                continue
            if not record and not job_id and self._expired(job):
                continue
            out.append((job.id, self.process_job(job, regenerate=regenerate)))
        return out

    # -------------------------------------------------------------- pin
    def _resolve(self, target: str) -> dict | None:
        record = self.repo.get(target)
        if record:
            return record
        rows = self.repo.for_job(target)
        return rows[0] if rows else None

    def mark_pinned(self, target: str, note: str = "", undo: bool = False) -> tuple[bool, str]:
        """사람이 Studio 에서 고정했다고 기록 (API 로 확인한 값이 아님을 함께 기록)."""
        record = self._resolve(target)
        if not record:
            return False, f"댓글 기록 없음: {target}"
        if record["status"] != CommentStatus.POSTED:
            return False, f"등록되지 않은 댓글은 고정 기록을 할 수 없습니다 (status={record['status']})"
        if undo:
            self.repo.upsert(record["youtube_video_id"], pin_status=PinStatus.MANUAL_REQUIRED, pin_recorded_at=None,
                             pin_note=note or "고정 기록 취소")
            return True, "고정 기록 취소 → manual_required"
        self.repo.upsert(record["youtube_video_id"], pin_status=PinStatus.PINNED_MANUAL, pin_recorded_at=now_iso(self.tz),
                         pin_note=(note or "운영자가 Studio 에서 고정했다고 기록") + " (API 확인 불가 항목)")
        return True, "pinned_manual 로 기록 (사람이 확인한 값, API 로 검증하지 않음)"

    def verify(self, record: dict) -> str:
        """등록한 댓글이 아직 조회되는지 확인 (고정 여부는 API 로 알 수 없으므로 바꾸지 않는다)."""
        if record.get("status") != CommentStatus.POSTED or not record.get("comment_id"):
            return record.get("status", "")
        items = self.service.comments().list(part="snippet", id=record["comment_id"]).execute().get("items", [])
        if items:
            return CommentStatus.POSTED
        self.repo.upsert(record["youtube_video_id"], status=CommentStatus.MISSING, pin_status=PinStatus.UNKNOWN,
                         error_code="not_found_on_verify", error_message="등록했던 댓글이 조회되지 않음 (삭제/보류?)")
        return CommentStatus.MISSING


def write_pin_guide(settings: Settings, db: Database, path: Path) -> Path:
    """수동 고정 운영 가이드 (등록됐지만 아직 고정 기록이 없는 댓글)."""
    repo, jobs = CommentRepository(db, settings.app.timezone), JobRepository(db, settings.app.timezone)
    rows = repo.list(statuses=[CommentStatus.POSTED], pin_statuses=[PinStatus.MANUAL_REQUIRED, PinStatus.UNKNOWN], limit=100)
    lines = [
        "# 고정 댓글 운영 가이드", "",
        f"생성: {now_iso(settings.app.timezone)}", "",
        "YouTube Data API 에는 댓글 고정 기능이 없습니다. 아래 댓글은 **등록만 완료**된 상태이며 고정 여부는 사람이 처리·기록합니다.", "",
        "## 수동 고정 절차",
        "1. 영상 URL 을 열고 로그인한 채널 계정으로 댓글 영역을 엽니다 (또는 YouTube Studio → 콘텐츠 → 영상 → 댓글).",
        "2. 아래 본문과 같은 채널 댓글 옆 ⋮(더보기) → **고정** → 확인. (영상당 고정 댓글은 1개, 기존 고정 댓글은 교체됩니다)",
        "3. 고정했으면 기록: `python -m app.main comments mark-pinned <영상ID> --note \"Studio 에서 고정\"`",
        "4. 고정하지 않기로 했다면 기록하지 않아도 됩니다 (상태는 manual_required 로 남음).", "",
        "## 처리 대기 목록", "",
    ]
    if not rows:
        lines.append("(없음)")
    for r in rows:
        job = jobs.get(r["job_id"]) if r.get("job_id") else None
        title = ((job.state.get("metadata") or {}).get("title") if job else "") or "-"
        lines += [
            f"### {title}",
            f"- 영상: https://youtube.com/shorts/{r['youtube_video_id']}  (작업 {r.get('job_id') or '-'})",
            f"- 댓글 ID: {r.get('comment_id') or '-'} / 등록: {r.get('posted_at') or '-'} / 고정 상태: {r['pin_status']}",
            f"- 본문: {r['comment_text']}", "",
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
