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

    def check(self, video_path: Path, subtitle_path: Path | None = None,
              expected_duration: float | None = None) -> QualityReport:
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
        if expected_duration:
            # 장면 영상(AI 영상/움직임 효과) 길이를 나레이션에 맞췄는지: 최종 길이 = 나레이션 + 여유
            checks.append(Check("duration_match", abs(info.duration - expected_duration) <= 0.35,
                                f"{info.duration:.2f}s / 예상 {expected_duration:.2f}s"))

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


def motion_checks(motion: dict | None, render_motion: dict | None, scene_numbers: list[int]) -> list[Check]:
    """이미지→영상 단계 검사 (video_motion 이 켜진 작업만). 실패하면 기존 검사와 같이 업로드 금지."""
    if not motion or not motion.get("enabled"):
        return []
    from app.video_motion.cache import file_sha256

    plan = {m["scene_number"]: m for m in motion.get("scenes", [])}
    used = {int(k): v for k, v in (render_motion or {}).items()}
    summary = motion.get("summary") or {}
    checks: list[Check] = []
    missing = [n for n in scene_numbers if n not in used]
    checks.append(Check("motion_sources", not missing, f"장면 영상 소스 없음: {missing}" if missing
                        else f"{len(used)}개 장면 모두 영상 소스 있음"))
    no_fallback = [n for n, m in plan.items() if m.get("source") == "ffmpeg" and used.get(n, {}).get("source") == "ai_video"]
    fallback = sum(1 for n, m in plan.items() if m.get("source") != "ai_video" and used.get(n, {}).get("source") in ("ffmpeg", "legacy"))
    checks.append(Check("motion_fallback", not no_fallback,
                        f"AI 영상 없는 장면 {fallback}개 FFmpeg 대체" + (f", 잘못된 소스: {no_fallback}" if no_fallback else "")))
    ai_planned = [n for n, m in plan.items() if m.get("source") == "ai_video"]
    ai_used = [n for n in ai_planned if used.get(n, {}).get("source") == "ai_video"]
    # AI 영상 합성에 실패한 장면은 렌더러가 움직임 효과로 다시 만들었으므로 실패가 아니라 기록만 한다
    checks.append(Check("motion_ai_composition", True, f"AI 영상 {len(ai_used)}/{len(ai_planned)}개 합성"
                        + (" (나머지는 움직임 효과로 대체)" if len(ai_used) < len(ai_planned) else "")))
    limits = summary.get("limits") or {}
    per_job = limits.get("per_job")
    over = per_job is not None and summary.get("requests", 0) > per_job
    after_stop = int(summary.get("requests_after_stop") or 0)
    checks.append(Check("motion_api_limits", not over and after_stop == 0,
                        f"요청 {summary.get('requests', 0)}/{per_job if per_job is not None else '-'}, 중단 후 요청 {after_stop}"))
    changed = []
    for n, m in plan.items():
        path, sha = m.get("image_path"), m.get("image_sha256")
        if path and sha and (not Path(path).exists() or file_sha256(path) != sha):
            changed.append(n)
    checks.append(Check("motion_images_preserved", not changed,
                        f"원본 AI 이미지 변경/삭제: {changed}" if changed else "원본 AI 이미지 보존"))
    return checks
