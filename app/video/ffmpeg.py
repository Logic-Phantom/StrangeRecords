"""FFmpeg 실행/미디어 분석 래퍼.

- ffmpeg: 시스템 PATH → imageio-ffmpeg 내장 바이너리 순으로 사용
- 분석: ffprobe 가 있으면 사용, 없으면 PyAV(faster-whisper 의존성)로 분석
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.utils.logger import get_logger

logger = get_logger("ffmpeg")


class FFmpegError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # pragma: no cover - 환경 의존
        raise FFmpegError("FFmpeg 를 찾을 수 없습니다. FFmpeg 설치 또는 `pip install imageio-ffmpeg`") from exc


@lru_cache(maxsize=1)
def ffprobe_bin() -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    sibling = Path(ffmpeg_bin()).with_name("ffprobe.exe" if ffmpeg_bin().endswith(".exe") else "ffprobe")
    return str(sibling) if sibling.exists() else None


def run_ffmpeg(args: list[str], *, step: str = "ffmpeg", timeout: int = 1800) -> str:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", *args]
    logger.debug("FFmpeg: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{step} 실패 (code {proc.returncode}):\n{tail}")
    return proc.stderr


@dataclass
class MediaInfo:
    path: str
    duration: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    audio_sample_rate: int = 0
    pix_fmt: str = ""
    size_bytes: int = 0


def _parse_rate(rate: str | None) -> float:
    if not rate or rate in ("0/0",):
        return 0.0
    if "/" in rate:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    return float(rate)


def _probe_ffprobe(path: Path, probe: str) -> MediaInfo:
    proc = subprocess.run(
        [probe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe 실패: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    info = MediaInfo(path=str(path), duration=float(data.get("format", {}).get("duration") or 0), size_bytes=path.stat().st_size)
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not info.has_video:
            info.has_video = True
            info.width, info.height = int(stream.get("width", 0)), int(stream.get("height", 0))
            info.fps = _parse_rate(stream.get("avg_frame_rate")) or _parse_rate(stream.get("r_frame_rate"))
            info.video_codec = stream.get("codec_name", "")
            info.pix_fmt = stream.get("pix_fmt", "")
        elif stream.get("codec_type") == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name", "")
            info.audio_sample_rate = int(stream.get("sample_rate", 0) or 0)
    return info


def _probe_pyav(path: Path) -> MediaInfo:
    import av

    with av.open(str(path)) as container:
        duration = float(container.duration / av.time_base) if container.duration else 0.0
        info = MediaInfo(path=str(path), duration=duration, size_bytes=path.stat().st_size)
        if container.streams.video:
            vs = container.streams.video[0]
            info.has_video = True
            info.width, info.height = vs.codec_context.width, vs.codec_context.height
            rate = vs.average_rate or vs.guessed_rate
            info.fps = float(rate) if rate else 0.0
            info.video_codec = vs.codec_context.name
            info.pix_fmt = vs.codec_context.pix_fmt or ""
            if not duration and vs.duration and vs.time_base:
                info.duration = float(vs.duration * vs.time_base)
        if container.streams.audio:
            aus = container.streams.audio[0]
            info.has_audio = True
            info.audio_codec = aus.codec_context.name
            info.audio_sample_rate = aus.codec_context.sample_rate or 0
            if not info.duration and aus.duration and aus.time_base:
                info.duration = float(aus.duration * aus.time_base)
    return info


def probe(path: Path | str) -> MediaInfo:
    path = Path(path)
    if not path.exists():
        raise FFmpegError(f"파일 없음: {path}")
    probe_bin = ffprobe_bin()
    if probe_bin:
        return _probe_ffprobe(path, probe_bin)
    return _probe_pyav(path)


def media_duration(path: Path | str) -> float:
    return probe(path).duration


_BLACK = re.compile(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)\s+black_duration:\s*([\d.]+)")


def detect_black(path: Path | str, min_duration: float = 0.5, pix_threshold: float = 0.10) -> list[tuple[float, float]]:
    stderr = run_ffmpeg(
        ["-i", str(path), "-vf", f"blackdetect=d={min_duration}:pix_th={pix_threshold}", "-an", "-f", "null", "-"],
        step="blackdetect",
    )
    return [(float(m.group(1)), float(m.group(2))) for m in _BLACK.finditer(stderr)]


def ffmpeg_version() -> str:
    proc = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.stdout.splitlines()[0] if proc.stdout else "unknown"


def filter_path(path: Path | str) -> str:
    """filtergraph 안에서 쓰는 경로 이스케이프 (Windows 드라이브 콜론 포함)."""
    return str(Path(path).as_posix()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def concat_list_path(path: Path | str) -> str:
    """concat demuxer 목록 파일용 경로 (작은따옴표 이스케이프)."""
    return Path(path).resolve().as_posix().replace("'", r"'\''")
