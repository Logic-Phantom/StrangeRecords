"""생성된 AI 영상 파일 검증 — 손상/길이 이상/영상 아님/검은 화면이면 정상 영상으로 쓰지 않는다."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.utils.logger import get_logger
from app.video.ffmpeg import detect_black, probe

logger = get_logger("motion_validator")

MIN_BYTES = 8 * 1024
MIN_SIDE = 256


def looks_like_video(data: bytes) -> bool:
    """MP4/MOV(ftyp), WebM/MKV(EBML) 시그니처 확인 (JSON 오류 본문이나 이미지가 영상으로 저장되지 않도록)."""
    head = data[:64]
    return (len(head) >= 12 and head[4:8] == b"ftyp") or head[:4] == b"\x1a\x45\xdf\xa3"


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    info: dict = field(default_factory=dict)


def validate_clip(path: Path | str, min_seconds: float = 1.0, max_seconds: float = 30.0,
                  check_black: bool = True, max_black_ratio: float = 0.5) -> ValidationResult:
    path = Path(path)
    if not path.exists():
        return ValidationResult(False, "파일 없음")
    size = path.stat().st_size
    if size < MIN_BYTES:
        return ValidationResult(False, f"파일이 너무 작음 ({size} bytes)")
    try:
        with open(path, "rb") as fp:
            if not looks_like_video(fp.read(64)):
                return ValidationResult(False, "영상 형식이 아님 (MP4/WebM 시그니처 없음)")
        info = probe(path)
    except Exception as exc:
        return ValidationResult(False, f"분석 실패(손상): {str(exc)[:200]}")
    base = dict(duration=info.duration, width=info.width, height=info.height, fps=info.fps,
                info={"codec": info.video_codec, "size_bytes": size})
    if not info.has_video:
        return ValidationResult(False, "영상 스트림 없음", **base)
    if info.width < MIN_SIDE or info.height < MIN_SIDE:
        return ValidationResult(False, f"해상도 이상 ({info.width}x{info.height})", **base)
    if info.duration < min_seconds:
        return ValidationResult(False, f"길이가 너무 짧음 ({info.duration:.2f}s < {min_seconds}s)", **base)
    if info.duration > max_seconds:
        return ValidationResult(False, f"길이 이상 ({info.duration:.2f}s > {max_seconds}s)", **base)
    if check_black:
        try:
            black = detect_black(path, min_duration=0.3)
            ratio = sum(e - s for s, e in black) / info.duration if info.duration else 1.0
            if ratio > max_black_ratio:
                return ValidationResult(False, f"검은 화면 비율 {ratio:.0%}", **base)
        except Exception as exc:
            return ValidationResult(False, f"프레임 검사 실패(손상): {str(exc)[:200]}", **base)
    return ValidationResult(True, "ok", **base)
