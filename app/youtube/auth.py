"""YouTube Data API v3 OAuth 2.0.

첫 실행: `python -m app.main auth` → 브라우저에서 Google 로그인 → 권한 승인 → token.json 저장
이후 자동 실행: 저장된 token.json 을 사용 (만료 시 refresh token 으로 자동 갱신)
"""

from __future__ import annotations

from pathlib import Path

from app.config.settings import Settings
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError

logger = get_logger("youtube.auth")

# 업로드 + 업로드 후 상태 확인에 필요한 기존 범위 (변경하지 않는다)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
# 성과 수집(YouTube Analytics API)용 읽기 범위. `auth` 명령에서 기존 범위와 함께 요청한다.
ANALYTICS_SCOPES = ["https://www.googleapis.com/auth/yt-analytics.readonly"]
AUTH_SCOPES = SCOPES + ANALYTICS_SCOPES


class YouTubeAuthError(NonRetryableError):
    pass


def _client_config(settings: Settings) -> dict | None:
    s = settings.secrets
    if s.youtube_client_id and s.youtube_client_secret:
        return {
            "installed": {
                "client_id": s.youtube_client_id,
                "client_secret": s.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        }
    return None


def token_path(settings: Settings) -> Path:
    return settings.paths.root / settings.youtube.token_file


def load_credentials(settings: Settings, interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(settings)
    creds = None
    if path.exists():
        import json

        info = json.loads(path.read_text(encoding="utf-8"))
        # 토큰에 기록된 범위를 그대로 사용한다. 여기서 SCOPES 로 덮어쓰면 갱신 후 저장할 때
        # Analytics 범위가 token.json 기록에서 빠지고, 반대로 받지 않은 범위를 넣으면 갱신이 거부될 수 있다.
        creds = Credentials.from_authorized_user_info(info, info.get("scopes") or SCOPES)

    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            path.write_text(creds.to_json(), encoding="utf-8")
            logger.info("YouTube 토큰 갱신 완료")
            return creds
        except Exception as exc:
            logger.warning("YouTube 토큰 갱신 실패: %s", exc)
            if not interactive:
                raise YouTubeAuthError(f"YouTube 토큰 갱신 실패 → `python -m app.main auth` 로 다시 인증하세요: {exc}") from exc

    if not interactive:
        raise YouTubeAuthError("YouTube 인증 정보가 없습니다. `python -m app.main auth` 를 먼저 실행하세요.")
    return run_oauth_flow(settings)


def run_oauth_flow(settings: Settings, scopes: list[str] | None = None):
    from google_auth_oauthlib.flow import InstalledAppFlow

    scopes = scopes or AUTH_SCOPES
    secrets_file = settings.paths.root / settings.youtube.client_secrets_file
    config = _client_config(settings)
    if config:
        flow = InstalledAppFlow.from_client_config(config, scopes)
    elif secrets_file.exists():
        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes)
    else:
        raise YouTubeAuthError(
            "YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET (.env) 또는 credentials.json 이 필요합니다."
        )
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_path(settings).write_text(creds.to_json(), encoding="utf-8")
    logger.info("YouTube 인증 완료 → %s 저장", settings.youtube.token_file)
    return creds


def build_service(settings: Settings, interactive: bool = False, scopes: list[str] | None = None):
    from googleapiclient.discovery import build

    if interactive and scopes:
        creds = run_oauth_flow(settings, scopes)
    else:
        creds = load_credentials(settings, interactive=interactive)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)
