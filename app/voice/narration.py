"""Scene 별 TTS 생성 → 하나의 나레이션 트랙으로 결합 → 실제 Scene 타이밍 계산.

Scene 마다 따로 합성하므로 각 Scene 의 정확한 시작/끝 시간을 얻을 수 있다.
전체 길이가 목표를 넘으면 말하기 속도를 올려 한 번 다시 합성한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.settings import Settings
from app.schemas import Scene, SceneTiming
from app.utils.logger import get_logger
from app.video.ffmpeg import media_duration, run_ffmpeg
from app.voice.base import VoiceProvider, format_percent, parse_percent

logger = get_logger("narration")


class NarrationTooShortError(Exception):
    pass


@dataclass
class NarrationResult:
    path: Path
    duration: float
    timings: list[SceneTiming]
    rate: str


def _concat(scene_files: list[Path], durations: list[float], out: Path, lead_in: float, gap: float, sample_rate: int) -> None:
    args: list[str] = []
    for f in scene_files:
        args += ["-i", str(f)]
    parts = []
    for i, _ in enumerate(scene_files):
        delay = int(lead_in * 1000) if i == 0 else 0
        pad = gap if i < len(scene_files) - 1 else 0
        chain = f"[{i}:a]aresample={sample_rate},aformat=sample_fmts=fltp:channel_layouts=mono"
        if delay:
            chain += f",adelay={delay}:all=1"
        chain += f",apad=pad_dur={pad:.3f}" if pad else ""
        parts.append(chain + f"[a{i}]")
    inputs = "".join(f"[a{i}]" for i in range(len(scene_files)))
    graph = ";".join(parts) + f";{inputs}concat=n={len(scene_files)}:v=0:a=1[out]"
    run_ffmpeg([*args, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", str(out)], step="narration concat")


def build_narration(
    scenes: list[Scene],
    provider: VoiceProvider,
    settings: Settings,
    out_dir: Path,
) -> NarrationResult:
    voice_cfg = settings.voice
    max_total = settings.quality.max_duration - settings.video.outro_padding - 0.3
    rate_value = parse_percent(voice_cfg.rate)
    base_rate = rate_value

    for attempt in range(2):
        rate = format_percent(rate_value)
        files: list[Path] = []
        durations: list[float] = []
        for scene in scenes:
            path = out_dir / f"scene_{scene.scene_number:02d}.mp3"
            provider.synthesize(scene.narration, path, rate=rate)
            files.append(path)
            durations.append(media_duration(path))

        total = voice_cfg.lead_in + sum(durations) + voice_cfg.scene_gap * (len(scenes) - 1)
        logger.info("Voice generated: %d scenes, %.1f초 (rate %s)", len(scenes), total, rate)
        if total <= max_total or attempt == 1:
            break
        needed = int((total / max_total - 1) * 100 * 1.08) + 1
        new_rate = min(rate_value + needed, base_rate + voice_cfg.max_rate_boost)
        if new_rate == rate_value:
            break
        logger.warning("나레이션 %.1f초 > 최대 %.1f초 → 속도 %s 로 재합성", total, max_total, format_percent(new_rate))
        rate_value = new_rate

    if total < settings.quality.min_duration - settings.video.outro_padding:
        raise NarrationTooShortError(f"나레이션이 너무 짧습니다: {total:.1f}초")

    out = out_dir / "narration.wav"
    _concat(files, durations, out, voice_cfg.lead_in, voice_cfg.scene_gap, settings.video.audio_sample_rate)

    timings: list[SceneTiming] = []
    cursor = 0.0
    for i, (scene, dur, f) in enumerate(zip(scenes, durations, files)):
        lead = voice_cfg.lead_in if i == 0 else 0.0
        gap = voice_cfg.scene_gap if i < len(scenes) - 1 else 0.0
        start = cursor
        speech_start = start + lead
        speech_end = speech_start + dur
        end = speech_end + gap
        timings.append(SceneTiming(scene_number=scene.scene_number, start=round(start, 3), end=round(end, 3),
                                   speech_start=round(speech_start, 3), speech_end=round(speech_end, 3), audio_path=str(f)))
        cursor = end
    return NarrationResult(path=out, duration=media_duration(out), timings=timings, rate=format_percent(rate_value))
