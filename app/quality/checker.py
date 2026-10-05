"""업로드 전 자동 품질 검사. 하나라도 실패하면 YouTube 업로드를 금지한다."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config.settings import QualityConfig
from app.utils.logger import get_logger
from app.video.ffmpeg import detect_black, probe

logger = get_logger("quality")


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class QualityReport:
    passed: bool
    checks: list[Check] = field(default_factory=list)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(c) for c in self.checks]}

    def summary(self) -> str:
        return ", ".join(f"{c.name}={'OK' if c.passed else 'FAIL'}" for c in self.checks)


class QualityChecker:
    def __init__(self, config: QualityConfig):
        self.config = config

    def check(self, video_path: Path, subtitle_path: Path | None = None) -> QualityReport:
        cfg = self.config
        checks: list[Check] = []
        exists = video_path.exists() and video_path.stat().st_size > 0
        checks.append(Check("file_exists", exists, str(video_path)))
        if not exists:
            return QualityReport(False, checks)

        info = probe(video_path)
        size_kb = info.size_bytes / 1024
        checks += [
            Check("duration", cfg.min_duration <= info.duration <= cfg.max_duration,
                  f"{info.duration:.2f}s (허용 {cfg.min_duration}~{cfg.max_duration})"),
            Check("resolution", info.width == cfg.width and info.height == cfg.height, f"{info.width}x{info.height}"),
            Check("fps", abs(info.fps - cfg.fps) < 0.5, f"{info.fps:.2f}"),
            Check("video_codec", info.video_codec == "h264", info.video_codec),
            Check("audio", info.has_audio, info.audio_codec or "없음"),
            Check("audio_codec", info.audio_codec == "aac", info.audio_codec),
            Check("file_size", cfg.min_file_size_kb <= size_kb <= cfg.max_file_size_mb * 1024, f"{size_kb / 1024:.1f}MB"),
        ]

        if subtitle_path is not None:
            has_sub = subtitle_path.exists() and subtitle_path.read_text(encoding="utf-8").strip() != ""
            checks.append(Check("subtitles", has_sub, str(subtitle_path.name)))

        try:
            black = detect_black(video_path, cfg.black_min_duration)
            black_total = sum(e - s for s, e in black)
            ratio = black_total / info.duration if info.duration else 1.0
            checks.append(Check("black_screen", ratio <= cfg.max_black_ratio, f"검은 화면 {black_total:.1f}s ({ratio:.0%})"))
        except Exception as exc:
            checks.append(Check("black_screen", False, f"검사 실패: {exc}"))

        report = QualityReport(all(c.passed for c in checks), checks)
        if report.passed:
            logger.info("Quality check passed (%s)", report.summary())
        else:
            for c in report.failures():
                logger.error("Quality check FAILED: %s → %s", c.name, c.detail)
        return report
