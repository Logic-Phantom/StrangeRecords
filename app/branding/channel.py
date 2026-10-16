"""채널 브랜딩: 자산 생성 / 홈페이지·약관·OAuth 범위 일관성 점검 / 채널 메타데이터 미리보기·적용.

공식 문서 확인 (2026-10-16)
- channels.update: part 는 brandingSettings | invideoPromotion | localizations 중 하나, 50 units,
  범위 youtube / youtube.force-ssl / youtubepartner. "요청에 값이 없는 속성은 기존 값이 삭제된다"
  → 반드시 현재 brandingSettings 를 읽어 그대로 두고 바꿀 필드만 덮어써서 보낸다. 제목(title)은 현재 값 유지.
  https://developers.google.com/youtube/v3/docs/channels/update
- channelBanners.insert: 16:9, 최소 2048x1152(권장 2560x1440), 6MB 이하, jpeg/png → 응답 url 을
  channels.update 의 brandingSettings.image.bannerExternalUrl 로 설정. https://developers.google.com/youtube/v3/docs/channelBanners/insert
- 프로필 사진: Data API 로 변경 불가 → 수동. 워터마크(watermarks.set)는 API 가 있지만 이 도구는 구현하지 않음 → 수동.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml
from PIL import Image, ImageDraw, ImageFilter

from app.config.settings import Settings
from app.utils.logger import get_logger

logger = get_logger("branding")

EDITABLE = ("description", "keywords", "defaultLanguage", "country")
BANNER_MIN, BANNER_MAX_MB = (2048, 1152), 6
ICON_MAX_MB, WATERMARK_MAX_MB = 15, 1


# ------------------------------------------------------------------ brand file
def brand_path(settings: Settings) -> Path:
    return settings.paths.root / settings.branding.brand_file


def output_dir(settings: Settings) -> Path:
    return settings.paths.root / settings.branding.output_dir


def load_brand(settings: Settings) -> dict:
    path = brand_path(settings)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def video_footer(settings: Settings) -> str:
    """영상 설명란에 붙일 채널 소개 한 줄 (없으면 빈 문자열)."""
    if not settings.branding.video_footer:
        return ""
    try:
        return str(load_brand(settings).get("video_footer") or "").strip()
    except Exception as exc:  # 브랜드 파일 문제로 업로드가 멈추지 않도록
        logger.warning("brand.yaml 읽기 실패 → 설명란 채널 소개 생략: %s", exc)
        return ""


def keywords_string(keywords: list[str]) -> str:
    """brandingSettings.channel.keywords 형식: 공백 구분, 공백이 있는 단어는 큰따옴표."""
    return " ".join(f'"{k}"' if " " in k else k for k in keywords if k)


# ------------------------------------------------------------------- assets
def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _font(settings: Settings, size: int):
    from app.video.subtitle_renderer import _font as load, find_font

    return load(find_font(settings.subtitle, settings.paths.fonts), size)


def _background(size: tuple[int, int], colors: dict) -> Image.Image:
    w, h = size
    bg, surface = _rgb(colors.get("background", "#0F1117")), _rgb(colors.get("surface", "#1A1D27"))
    img = Image.new("RGB", size, bg)
    glow = Image.new("L", size, 0)
    ImageDraw.Draw(glow).ellipse((w * 0.15, h * 0.05, w * 0.85, h * 0.95), fill=255)
    glow = glow.filter(ImageFilter.GaussianBlur(min(w, h) // 5))
    img.paste(Image.new("RGB", size, surface), mask=glow)
    # 기록 보관소 느낌의 옅은 가로줄
    draw = ImageDraw.Draw(img)
    for y in range(0, h, max(h // 48, 6)):
        draw.line((0, y, w, y), fill=tuple(min(c + 4, 255) for c in bg), width=1)
    return img


def _ring(draw: ImageDraw.ImageDraw, box: tuple[float, float, float, float], color, width: int) -> None:
    draw.ellipse(box, outline=color, width=width)
    # 녹화 표시 같은 작은 점 (기록)
    x0, y0, x1, _ = box
    r = width * 1.2
    cx, cy = x1 - (x1 - x0) * 0.15, y0 + (x1 - x0) * 0.15
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)


def _center_text(draw: ImageDraw.ImageDraw, text: str, cx: float, cy: float, font, fill) -> None:
    box = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (box[2] - box[0]) / 2 - box[0], cy - (box[3] - box[1]) / 2 - box[1]), text, font=font, fill=fill)


def generate_assets(settings: Settings) -> list[Path]:
    """brand.yaml 색상/이름으로 아이콘·배너·워터마크 PNG 생성 (채널에는 적용하지 않음)."""
    brand = load_brand(settings)
    if not brand:
        raise FileNotFoundError(f"브랜드 파일 없음: {brand_path(settings)}")
    colors, assets = brand.get("colors", {}), brand.get("assets", {})
    name = (brand.get("channel") or {}).get("name") or settings.app.name
    accent, ink, muted = _rgb(colors.get("accent", "#FFD43B")), _rgb(colors.get("ink", "#E6E6E6")), _rgb(colors.get("muted", "#9AA0A6"))
    out = output_dir(settings)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    # 아이콘 800x800: 원형 링 + 두 줄 이름 (98x98 로 줄어도 읽히도록 굵고 크게)
    size = tuple(assets.get("icon", {}).get("size", [800, 800]))
    icon = _background(size, colors)
    d = ImageDraw.Draw(icon)
    w, h = size
    _ring(d, (w * 0.08, h * 0.08, w * 0.92, h * 0.92), accent, max(w // 28, 6))
    words = name.split()
    lines = [words[0], " ".join(words[1:])] if len(words) > 1 else [name]
    font = _font(settings, int(h * 0.2))
    for i, line in enumerate(lines):
        _center_text(d, line, w / 2, h * (0.40 + 0.22 * i) if len(lines) > 1 else h / 2, font, accent if i else ink)
    paths.append(_save(icon, out / assets.get("icon", {}).get("file", "icon_800.png")))

    # 배너 2560x1440: 글자는 안전 영역(가운데 1546x423 @2560) 안에만
    size = tuple(assets.get("banner", {}).get("size", [2560, 1440]))
    banner = _background(size, colors)
    d = ImageDraw.Draw(banner)
    w, h = size
    safe_w, safe_h = w * 1235 / 2048, h * 338 / 1152
    sx0, sy0 = (w - safe_w) / 2, (h - safe_h) / 2
    d.line((sx0 + safe_w * 0.08, sy0 + safe_h * 0.70, sx0 + safe_w * 0.92, sy0 + safe_h * 0.70), fill=accent, width=4)
    _center_text(d, name, w / 2, sy0 + safe_h * 0.36, _font(settings, int(safe_h * 0.42)), ink)
    _center_text(d, "기록으로 확인된 사실 · 전해지는 이야기", w / 2, sy0 + safe_h * 0.86, _font(settings, int(safe_h * 0.15)), muted)
    paths.append(_save(banner, out / assets.get("banner", {}).get("file", "banner_2560x1440.png")))

    # 워터마크 150x150: 투명 배경 링 + '기록'
    size = tuple(assets.get("watermark", {}).get("size", [150, 150]))
    mark = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(mark)
    w, h = size
    d.ellipse((4, 4, w - 4, h - 4), fill=(*_rgb(colors.get("background", "#0F1117")), 200), outline=accent, width=6)
    _center_text(d, (words[-1] if words else name)[:2], w / 2, h / 2, _font(settings, int(h * 0.34)), accent)
    paths.append(_save(mark, out / assets.get("watermark", {}).get("file", "watermark_150.png")))
    return paths


def _save(img: Image.Image, path: Path) -> Path:
    img.save(path, optimize=True)
    return path


# -------------------------------------------------------------- consistency
def _item(name: str, level: str, detail: str) -> dict:
    return {"name": name, "level": level, "detail": detail}


def check_consistency(settings: Settings) -> list[dict]:
    """홈페이지·개인정보처리방침·약관·OAuth 범위·브랜드 파일·자산 규격 점검 (API 호출 없음, 파일만 읽음)."""
    from app.youtube.auth import requested_scopes

    out: list[dict] = []
    docs = settings.paths.root / "docs"
    pages = {name: (docs / name) for name in ("index.html", "privacy.html", "terms.html")}
    text: dict[str, str] = {}
    for name, path in pages.items():
        if path.exists():
            text[name] = path.read_text(encoding="utf-8")
            out.append(_item(f"docs/{name}", "ok", "존재"))
        else:
            out.append(_item(f"docs/{name}", "fail", "없음 (OAuth 앱 홈페이지/정책 URL 이 깨짐)"))
    if not text:
        return out

    # 페이지 간 링크
    for src, targets in (("index.html", ("privacy.html", "terms.html")), ("privacy.html", ("index.html",)),
                         ("terms.html", ("index.html", "privacy.html"))):
        missing = [t for t in targets if f'href="{t}"' not in text.get(src, "")]
        out.append(_item(f"링크 {src}", "warn" if missing else "ok", f"없는 링크: {', '.join(missing)}" if missing else "정상"))

    # 앱 이름
    name = settings.app.name
    no_name = [n for n, t in text.items() if name not in t]
    out.append(_item("앱 이름", "warn" if no_name else "ok", f"'{name}' 없음: {', '.join(no_name)}" if no_name else f"'{name}' 일치"))

    # OAuth 범위: `auth` 가 요청하는 범위가 개인정보처리방침에 모두 적혀 있어야 한다 (실제 데이터 처리와 일치)
    privacy = text.get("privacy.html", "")
    for scope in requested_scopes():
        short = scope.rsplit("/", 1)[-1]
        out.append(_item(f"범위 {short}", "ok" if short in privacy else "fail",
                         "개인정보처리방침에 기재" if short in privacy else "개인정보처리방침에 없음 → 실제 요청 범위와 불일치"))
    # 저장하는 데이터
    for keyword, why, needed in (
        ("Analytics", "성과 지표(Analytics) 저장", settings.analytics.enabled),
        ("댓글", "자동 댓글 본문/ID 저장", True),
        ("Cloudflare", "AI 이미지 제공자(Cloudflare Workers AI)", settings.assets.cf_image.enabled),
    ):
        if needed:
            out.append(_item(f"기재: {why}", "ok" if keyword in privacy else "warn",
                             "개인정보처리방침에 기재" if keyword in privacy else f"'{keyword}' 언급 없음"))

    # 브랜드 파일 / 채널 소개
    brand = load_brand(settings)
    if not brand:
        out.append(_item("brand.yaml", "fail", f"없음: {brand_path(settings)}"))
        return out
    ch = brand.get("channel") or {}
    out.append(_item("brand.yaml 채널 이름", "ok" if ch.get("name") == name else "warn",
                     f"{ch.get('name')} / config app.name={name}"))
    desc = ch.get("description", "")
    out.append(_item("채널 소개 길이", "ok" if 0 < len(desc) <= 1000 else "fail", f"{len(desc)}자 (YouTube 최대 1000자)"))
    out.append(_item("채널 소개 AI 고지", "ok" if "AI" in desc else "warn", "AI 활용 고지 포함" if "AI" in desc else "AI 활용 언급 없음"))
    homepage = (brand.get("links") or {}).get("homepage", "")
    out.append(_item("홈페이지 URL", "ok" if homepage.startswith("https://") else "warn", homepage or "없음"))

    # 출력 자산 규격
    assets = brand.get("assets") or {}
    for key, rule in (("banner", "banner"), ("icon", "icon"), ("watermark", "watermark")):
        conf = assets.get(key) or {}
        path = output_dir(settings) / conf.get("file", "")
        if not conf.get("file") or not path.exists():
            out.append(_item(f"자산 {key}", "warn", "출력 파일 없음 → python -m app.main branding assets"))
            continue
        with Image.open(path) as im:
            w, h = im.size
        mb = path.stat().st_size / 1024 / 1024
        problems = []
        if rule == "banner":
            if (w, h) < BANNER_MIN or w < BANNER_MIN[0] or h < BANNER_MIN[1]:
                problems.append(f"최소 {BANNER_MIN[0]}x{BANNER_MIN[1]} 미만")
            if abs(w / h - 16 / 9) > 0.01:
                problems.append("16:9 아님")
            if mb > BANNER_MAX_MB:
                problems.append(f"{mb:.1f}MB > 6MB")
        elif rule == "icon":
            if w != h:
                problems.append("정사각형 아님")
            if mb > ICON_MAX_MB:
                problems.append(f"{mb:.1f}MB > 15MB")
        else:
            if w != h or w < 150:
                problems.append("정사각형 150px 이상 아님")
            if mb >= WATERMARK_MAX_MB:
                problems.append(f"{mb:.2f}MB ≥ 1MB")
        out.append(_item(f"자산 {key}", "fail" if problems else "ok",
                         f"{w}x{h} {mb:.2f}MB" + (f" → {', '.join(problems)}" if problems else f" (적용: {conf.get('apply')})")))
    return out


# ------------------------------------------------------------- channel sync
def summarize(item: dict) -> dict:
    ch = (item.get("brandingSettings") or {}).get("channel") or {}
    return {
        "id": item.get("id"), "title": (item.get("snippet") or {}).get("title"),
        "description": ch.get("description", ""), "keywords": ch.get("keywords", ""),
        "defaultLanguage": ch.get("defaultLanguage", ""), "country": ch.get("country", ""),
        "has_banner": bool((item.get("brandingSettings") or {}).get("image")),
    }


class ChannelBranding:
    def __init__(self, settings: Settings, service_factory: Callable[[], Any] | None = None):
        self.settings = settings
        self._factory = service_factory
        self._service = None

    @property
    def service(self):
        if self._service is None:
            if self._factory:
                self._service = self._factory()
            else:
                from app.youtube.auth import build_service

                self._service = build_service(self.settings, interactive=False)
        return self._service

    def current(self) -> dict:
        items = self.service.channels().list(part="snippet,brandingSettings", mine=True).execute().get("items", [])
        if not items:
            raise RuntimeError("인증된 계정의 채널을 찾을 수 없음")
        return items[0]

    def desired(self) -> dict:
        ch = load_brand(self.settings).get("channel") or {}
        out = {}
        if ch.get("description"):
            out["description"] = ch["description"].strip()
        if ch.get("keywords"):
            out["keywords"] = keywords_string(ch["keywords"])
        if ch.get("default_language"):
            out["defaultLanguage"] = ch["default_language"]
        if ch.get("country"):
            out["country"] = ch["country"]
        return out

    def plan(self) -> dict:
        current = self.current()
        branding = copy.deepcopy(current.get("brandingSettings") or {})
        branding.pop("hints", None)  # 읽기 전용(사용 중단) 항목
        channel = branding.setdefault("channel", {})
        changes = []
        for key, value in self.desired().items():
            if key in EDITABLE and channel.get(key, "") != value:
                changes.append({"field": key, "old": channel.get(key, ""), "new": value})
                channel[key] = value
        # 제목은 바꾸지 않는다 (현재 값 그대로 보내야 함)
        if (current.get("snippet") or {}).get("title"):
            channel["title"] = current["snippet"]["title"]
        return {
            "channel_id": current["id"], "current": summarize(current), "changes": changes, "body": branding,
            "manual": [
                "프로필 사진: Studio > 맞춤설정 > 브랜딩 > 사진 → assets/branding/output/icon_800.png (API 미지원)",
                "워터마크: Studio > 맞춤설정 > 브랜딩 > 동영상 워터마크 → assets/branding/output/watermark_150.png",
            ],
        }

    def apply(self, plan: dict, banner: bool = False) -> dict:
        """미리보기에서 확인한 plan 을 적용. 현재 brandingSettings 전체를 유지한 채 바꿀 필드만 덮어쓴다."""
        result: dict = {"ok": False, "applied": [], "manual_required": plan["manual"], "verified": {}}
        body = copy.deepcopy(plan["body"])
        if banner:
            from googleapiclient.http import MediaFileUpload

            conf = (load_brand(self.settings).get("assets") or {}).get("banner") or {}
            path = output_dir(self.settings) / conf.get("file", "banner_2560x1440.png")
            if not path.exists():
                result["error"] = f"배너 파일 없음: {path} → branding assets 먼저 실행"
                return self._log(result)
            resp = self.service.channelBanners().insert(
                media_body=MediaFileUpload(str(path), mimetype="image/png", resumable=False)).execute()
            body.setdefault("image", {})["bannerExternalUrl"] = resp["url"]
            result["applied"].append("banner(uploaded)")
        if not plan["changes"] and not banner:
            result.update(ok=True, message="변경 사항 없음 → API 를 호출하지 않음")
            return self._log(result)
        try:
            self.service.channels().update(part="brandingSettings",
                                           body={"id": plan["channel_id"], "brandingSettings": body}).execute()
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
            return self._log(result)
        result["applied"] += [c["field"] for c in plan["changes"]]
        # 적용 후 다시 읽어 실제로 바뀌었는지 확인 (확인되지 않으면 성공으로 기록하지 않음)
        after = summarize(self.current())
        for change in plan["changes"]:
            result["verified"][change["field"]] = after.get(change["field"]) == change["new"]
        result["ok"] = all(result["verified"].values()) if result["verified"] else True
        return self._log(result)

    def _log(self, result: dict) -> dict:
        stamp = datetime.now(ZoneInfo(self.settings.app.timezone)).strftime("%Y%m%d-%H%M%S")
        path = self.settings.paths.reports / f"branding_apply_{stamp}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["log"] = str(path)
        return result


def format_plan(plan: dict) -> str:
    lines = [f"채널 {plan['current'].get('title')} ({plan['channel_id']})"]
    if not plan["changes"]:
        lines.append("  변경할 채널 정보 없음 (brand.yaml 과 동일)")
    for c in plan["changes"]:
        old = re.sub(r"\s+", " ", str(c["old"]))[:80] or "(비어 있음)"
        new = re.sub(r"\s+", " ", str(c["new"]))[:80]
        lines.append(f"  ~ {c['field']}: {old}\n      → {new}")
    kept = [k for k in (plan["body"].get("channel") or {}) if k not in {c["field"] for c in plan["changes"]}]
    lines.append(f"  유지되는 기존 항목: {', '.join(kept) or '-'}")
    lines.append("수동 처리 필요 (API 미지원/미구현):")
    lines += [f"  - {m}" for m in plan["manual"]]
    return "\n".join(lines)
