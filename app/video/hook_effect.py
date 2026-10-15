"""첫 장면 앞부분(기본 2.5초)에만 거는 시각 Hook 효과 — 안전한 필터 구성기.

- 사용자 문자열을 FFmpeg 에 넣지 않는다. 프리셋 이름 → 미리 정의한 구성 요소 → 숫자만 바꾼 필터 문자열.
- 첫 Scene 클립을 만들 때만 적용한다 (자막은 나중에 overlay 되므로 자막은 흔들리거나 번지지 않는다).
  이후 Scene, xfade 전환, 자막, 오디오/BGM 덕킹/음량 정규화 그래프는 바뀌지 않는다.
- 흔들림: 프레임을 (W+2A)x(H+2A) 로 키운 뒤 W x H 로 잘라 움직이므로 프레임 경계(검은 테두리)가 보이지 않는다.
- 글리치: 0.12초짜리 RGB 분리를 최대 3번, 0.6초 이상 간격으로만 (빠른 점멸 반복 없음).
- 효과 세기는 시간이 지날수록 줄어들어 duration 이후에는 원래 화면과 같다.
- 사용 중인 FFmpeg 에 필요한 필터가 없으면 그 구성 요소만 빼고, 렌더링이 실패하면 효과 없이 다시 만든다(renderer).
"""

from __future__ import annotations

import random
import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from app.config.settings import HookEffectConfig, VideoConfig
from app.utils.logger import get_logger

logger = get_logger("hook_effect")

PRESETS: dict[str, list[str]] = {
    "subtle": ["shake_light", "punch", "noise_light"],
    "vhs": ["punch", "chroma", "noise_vhs"],
    "glitch": ["shake_light", "punch", "glitch"],
    "impact": ["shake_strong", "punch_strong"],
    "none": [],
}
# 구성 요소 → 필요한 FFmpeg 필터 / 허용 스위치 종류
REQUIRED_FILTERS: dict[str, tuple[str, ...]] = {
    "shake_light": ("scale", "crop"), "shake_strong": ("scale", "crop"),
    "punch": ("eq",), "punch_strong": ("eq",),
    "noise_light": ("noise",), "noise_vhs": ("noise",),
    "chroma": ("rgbashift",), "glitch": ("rgbashift",),
}
KIND = {
    "shake_light": "shake", "shake_strong": "shake", "noise_light": "noise", "noise_vhs": "noise",
    "chroma": "glitch", "glitch": "glitch", "punch": "punch", "punch_strong": "punch",
}
_FILTER_LINE = re.compile(r"^\s*[TSC.]{2,3}\s+(\w+)\s+\S+->\S+", re.M)


@lru_cache(maxsize=4)
def available_filters(ffmpeg: str | None = None) -> frozenset[str]:
    from app.video.ffmpeg import ffmpeg_bin

    try:
        proc = subprocess.run([ffmpeg or ffmpeg_bin(), "-hide_banner", "-filters"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return frozenset(_FILTER_LINE.findall(proc.stdout))
    except Exception as exc:  # 확인 실패 시 효과 없이 렌더링
        logger.warning("FFmpeg 필터 목록 확인 실패 → Hook 효과 사용 안 함: %s", exc)
        return frozenset()


@dataclass
class HookEffectPlan:
    preset: str
    duration: float
    components: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    filter: str = ""

    def to_dict(self) -> dict:
        return {"preset": self.preset, "duration": self.duration, "components": self.components,
                "skipped": self.skipped, "filter": self.filter}


def _fade(d: float) -> str:
    # 1 → 0 으로 줄어드는 세기 (d 초 뒤 0)
    return f"max(0,1-t/{d:.3f})"


def _component(name: str, d: float, w: int, h: int, rng: random.Random) -> str:
    fade = _fade(d)
    if name in ("shake_light", "shake_strong"):
        a = 6 if name == "shake_light" else 14
        fx, fy = rng.uniform(5.5, 8.5), rng.uniform(4.5, 7.5)
        px, py = rng.uniform(0, 6.28), rng.uniform(0, 6.28)
        return (f"scale={w + 2 * a}:{h + 2 * a}:flags=bicubic,setsar=1,"
                f"crop={w}:{h}:x='{a}+{a}*sin({fx:.2f}*t+{px:.2f})*{fade}':y='{a}+{a}*cos({fy:.2f}*t+{py:.2f})*{fade}'")
    if name in ("punch", "punch_strong"):
        c, b = (0.18, 0.04) if name == "punch" else (0.3, 0.06)
        return f"eq=contrast='1+{c}*{fade}':brightness='{b}*{fade}':eval=frame"
    if name == "noise_light":
        return f"noise=alls=10:allf=t:enable='lt(t,{d:.3f})'"
    if name == "noise_vhs":
        return f"noise=alls=18:allf=t+u:enable='lt(t,{d:.3f})'"
    if name == "chroma":
        return f"rgbashift=rh=-3:bh=3:enable='lt(t,{d:.3f})'"
    if name == "glitch":
        pulses: list[float] = []
        t = rng.uniform(0.2, 0.5)
        while t + 0.12 < d - 0.1 and len(pulses) < 3:
            pulses.append(round(t, 2))
            t += rng.uniform(0.6, 1.0)
        if not pulses:
            return ""
        window = "+".join(f"between(t,{p:.2f},{p + 0.12:.2f})" for p in pulses)
        return f"rgbashift=rh=-8:bh=8:gh=2:enable='{window}'"
    raise ValueError(name)


def build_hook_effect(cfg: HookEffectConfig, video: VideoConfig, scene_duration: float, seed: str = "",
                      available: frozenset[str] | set[str] | None = None) -> HookEffectPlan | None:
    """첫 Scene 클립의 -vf 끝에 덧붙일 필터 문자열. 쓸 수 있는 효과가 없으면 None."""
    if not cfg.enabled:
        return None
    preset = cfg.preset if cfg.preset in PRESETS else "subtle"
    if cfg.preset not in PRESETS:
        logger.warning("알 수 없는 hook_effect.preset '%s' → subtle", cfg.preset)
    names = PRESETS[preset]
    if not names:
        return None
    duration = round(min(cfg.duration_seconds, max(scene_duration - 0.1, 0.5)), 3)
    available = available_filters() if available is None else available
    allow = {"shake": cfg.allow_shake, "noise": cfg.allow_noise, "glitch": cfg.allow_glitch, "punch": True}
    rng = random.Random(seed if cfg.deterministic_seed else None)
    plan = HookEffectPlan(preset=preset, duration=duration)
    parts: list[str] = []
    for name in names:
        if not allow[KIND[name]]:
            plan.skipped[name] = f"allow_{KIND[name]}=false"
            continue
        missing = [f for f in REQUIRED_FILTERS[name] if f not in available]
        if missing:
            plan.skipped[name] = f"FFmpeg 필터 없음: {', '.join(missing)}"
            continue
        part = _component(name, duration, video.width, video.height, rng)
        if part:
            parts.append(part)
            plan.components.append(name)
    if not parts:
        return None
    plan.filter = ",".join(parts)
    return plan
