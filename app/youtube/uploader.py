"""YouTube 업로드 + 예약 공개(publishAt)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.schemas import VideoMetadata
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError, retry_call
from app.youtube.auth import build_service

logger = get_logger("youtube")

RETRIABLE_STATUS = {500, 502, 503, 504}


@dataclass
class PublishPlan:
    privacy_status: str
    publish_at: datetime | None

    @property
    def publish_at_iso(self) -> str | None:
        return self.publish_at.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.000Z") if self.publish_at else None


@dataclass
class UploadResult:
    video_id: str
    url: str
    privacy_status: str
    publish_at: str | None


def plan_publish(settings: Settings, now: datetime | None = None) -> PublishPlan:
    """publish_mode 와 현재 시각으로 공개 방식을 결정한다.

    scheduled: 오늘 publish_time(예: 12:00 KST) 예약 공개.
      이미 지났으면 late_policy 에 따라 public_now(즉시 공개) / next_day(내일 같은 시간) / private.
    """
    yt = settings.youtube
    tz = ZoneInfo(settings.schedule.timezone)
    now = now or datetime.now(tz)
    mode = yt.publish_mode.lower()

    if mode == "public":
        return PublishPlan("public", None)
    if mode != "scheduled":
        return PublishPlan(yt.privacy_status, None)

    hour, minute = (int(x) for x in yt.publish_time.split(":"))
    target = now.astimezone(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target - now >= timedelta(minutes=yt.min_schedule_margin_minutes):
        return PublishPlan("private", target)  # YouTube 예약 공개는 private + publishAt

    policy = yt.late_policy.lower()
    if policy == "next_day":
        return PublishPlan("private", target + timedelta(days=1))
    if policy == "public_now":
        return PublishPlan("public", None)
    return PublishPlan("private", None)


class YouTubeUploader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def upload(self, video_path: Path, metadata: VideoMetadata, plan: PublishPlan | None = None) -> UploadResult:
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload

        yt = self.settings.youtube
        plan = plan or plan_publish(self.settings)
        service = build_service(self.settings, interactive=False)

        status: dict = {
            "privacyStatus": plan.privacy_status,
            "selfDeclaredMadeForKids": yt.made_for_kids,
            "containsSyntheticMedia": yt.contains_synthetic_media,
        }
        if plan.publish_at:
            status["publishAt"] = plan.publish_at_iso
        body = {
            "snippet": {
                "title": metadata.title[:100],
                "description": metadata.description,
                "tags": metadata.tags,
                "categoryId": yt.category_id,
                "defaultLanguage": yt.default_language,
                "defaultAudioLanguage": yt.default_language,
            },
            "status": status,
        }

        logger.info("YouTube upload started (%s%s)", plan.privacy_status, f", publishAt={plan.publish_at}" if plan.publish_at else "")

        def _upload() -> dict:
            media = MediaFileUpload(str(video_path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            try:
                while response is None:
                    progress, response = request.next_chunk(num_retries=3)
                    if progress:
                        logger.info("업로드 진행 %d%%", int(progress.progress() * 100))
            except HttpError as exc:
                if exc.resp.status in RETRIABLE_STATUS:
                    raise
                raise NonRetryableError(f"YouTube 업로드 거부 ({exc.resp.status}): {exc}") from exc
            return response

        response = retry_call(_upload, step="YouTube Upload", max_attempts=self.settings.retry.max_attempts, delays=self.settings.retry.delays)
        video_id = response["id"]
        url = f"https://youtube.com/shorts/{video_id}"
        logger.info("Upload completed: %s", url)
        return UploadResult(video_id, url, plan.privacy_status, plan.publish_at.isoformat() if plan.publish_at else None)
