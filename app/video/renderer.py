"""최종 영상 렌더링: Scene 클립 → 전환 효과로 연결 → 자막 + 나레이션 + BGM + 효과음 합성.

출력: 1080x1920 / 9:16 / 30fps / H.264(yuv420p) / AAC 48kHz / MP4(faststart)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.assets.audio_library import AudioLibrary
from app.config.settings import Settings
from app.schemas import AssetInfo, AudioTrackInfo, Scene, SceneTiming, SubtitleChunk
from app.utils.logger import get_logger
from app.video.ffmpeg import run_ffmpeg
from app.video.hook_effect import build_hook_effect
from app.video.scenes import build_scene_clip
from app.video.subtitle_renderer import SubtitleRenderer

logger = get_logger("renderer")


@dataclass
class RenderResult:
    video_path: Path
    duration: float
    thumbnail_path: Path | None
    sfx_used: list[dict]
    hook_effect: dict | None = None


def scene_durations(timings: list[SceneTiming], total: float) -> list[float]:
    durations = [t.end - t.start for t in timings]
    durations[-1] = total - timings[-1].start
    return durations


class VideoRenderer:
    def __init__(self, settings: Settings, audio_library: AudioLibrary):
        self.settings = settings
        self.cfg = settings.video
        self.audio_cfg = settings.audio
        self.library = audio_library
        self.subtitles = SubtitleRenderer(settings.subtitle, settings.video, settings.paths.fonts)
        self.hook_effect: dict = {"preset": "none", "applied": False}

    # ------------------------------------------------------------ video
    def _join(self, clips: list[Path], durations: list[float], out: Path) -> Path:
        args: list[str] = []
        for clip in clips:
            args += ["-i", str(clip)]
        n = len(clips)
        use_xfade = self.cfg.transition == "fade" and n > 1
        if use_xfade:
            t = self.cfg.transition_duration
            parts, prev, offset = [], "[0:v]", 0.0
            for i in range(1, n):
                offset += durations[i - 1]
                label = f"[x{i}]" if i < n - 1 else "[v]"
                parts.append(
                    f"{prev}[{i}:v]xfade=transition={self.cfg.transition_type}:duration={t}:offset={offset:.3f}{label}"
                )
                prev = label
            graph = ";".join(parts)
        else:
            graph = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
        run_ffmpeg(
            [*args, "-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
             "-pix_fmt", "yuv420p", "-r", str(self.cfg.fps), str(out)],
            step="scene join",
        )
        return out

    def _first_clip_with_hook(self, asset: AssetInfo, duration: float, effect: str, clip: Path, seed: str,
                              scene_length: float) -> None:
        """첫 Scene 에만 Hook 효과. 필터가 없거나 렌더링이 실패하면 효과 없이 다시 만든다 (제작은 멈추지 않음)."""
        plan = build_hook_effect(self.cfg.hook_effect, self.cfg, scene_length, seed=seed)
        if not plan:
            self.hook_effect = {"preset": "none", "applied": False,
                                "reason": "비활성" if not self.cfg.hook_effect.enabled else "사용 가능한 효과 없음"}
            build_scene_clip(asset, duration, effect, clip, self.cfg)
            return
        try:
            build_scene_clip(asset, duration, effect, clip, self.cfg, extra_filter=plan.filter)
            self.hook_effect = {**plan.to_dict(), "applied": True}
            logger.info("첫 장면 Hook 효과 적용: %s (%s, %.1f초)", plan.preset, "+".join(plan.components), plan.duration)
        except Exception as exc:
            logger.warning("Hook 효과 렌더링 실패 → 효과 없이 첫 장면 생성: %s", str(exc).splitlines()[0])
            self.hook_effect = {**plan.to_dict(), "applied": False, "reason": f"렌더링 실패: {str(exc)[:300]}"}
            build_scene_clip(asset, duration, effect, clip, self.cfg)

    def build_base_video(self, scenes: list[Scene], assets: list[AssetInfo], timings: list[SceneTiming], total: float,
                         work: Path, hook_seed: str = "") -> Path:
        asset_map = {a.scene_number: a for a in assets}
        durations = scene_durations(timings, total)
        use_xfade = self.cfg.transition == "fade" and len(scenes) > 1
        clips: list[Path] = []
        for i, (scene, duration) in enumerate(zip(scenes, durations)):
            # xfade 는 겹치는 만큼 앞 클립이 길어야 전체 길이와 Scene 시작 시간이 유지된다
            extra = self.cfg.transition_duration if use_xfade and i < len(scenes) - 1 else 0.0
            clip = work / f"clip_{scene.scene_number:02d}.mp4"
            if i == 0:
                # 효과 길이는 첫 Scene 의 실제 길이(전환 겹침 제외) 안으로 제한
                self._first_clip_with_hook(asset_map[scene.scene_number], duration + extra, scene.effect, clip,
                                           hook_seed or str(scene.scene_number), scene_length=duration)
            else:
                build_scene_clip(asset_map[scene.scene_number], duration + extra, scene.effect, clip, self.cfg)
            clips.append(clip)
        logger.info("Scene 클립 %d개 생성", len(clips))
        return self._join(clips, durations, work / "base.mp4")

    # ------------------------------------------------------------ final
    def render(
        self,
        scenes: list[Scene],
        assets: list[AssetInfo],
        timings: list[SceneTiming],
        narration: Path,
        narration_duration: float,
        chunks: list[SubtitleChunk],
        bgm: AudioTrackInfo | None,
        title: str,
        out_path: Path,
        work: Path,
        thumbnail_path: Path | None = None,
        hook_seed: str = "",
        info_cards: list | None = None,
    ) -> RenderResult:
        logger.info("Rendering started")
        work.mkdir(parents=True, exist_ok=True)
        total = round(narration_duration + self.cfg.outro_padding, 3)
        base = self.build_base_video(scenes, assets, timings, total, work, hook_seed=hook_seed)
        sub_list = self.subtitles.build_track(chunks, total, work / "subs", title=title, cards=info_cards,
                                              ai_label=any(a.generated_by_ai for a in assets))

        sr = self.cfg.audio_sample_rate
        fmt = f"aformat=sample_fmts=fltp:sample_rates={sr}:channel_layouts=stereo"
        inputs = ["-i", str(base), "-i", str(narration), "-f", "concat", "-safe", "0", "-i", str(sub_list)]
        idx = 3
        audio_parts: list[str] = []
        mix_labels: list[str] = []

        ducking = bool(bgm and self.audio_cfg.bgm_enabled and self.audio_cfg.bgm_ducking)
        voice_chain = f"[1:a]{fmt},apad=whole_dur={total}"
        if ducking:
            audio_parts.append(voice_chain + ",asplit=2[voice][vsc]")
        else:
            audio_parts.append(voice_chain + "[voice]")
        mix_labels.append("[voice]")

        if bgm and self.audio_cfg.bgm_enabled:
            inputs += ["-stream_loop", "-1", "-i", bgm.file]
            fade_out_start = max(total - 2.0, 0)
            chain = (
                f"[{idx}:a]{fmt},atrim=0:{total},asetpts=PTS-STARTPTS,volume={self.audio_cfg.bgm_volume},"
                f"afade=t=in:d=1.0,afade=t=out:st={fade_out_start:.3f}:d=2.0"
            )
            if ducking:
                audio_parts.append(chain + "[bgmraw]")
                audio_parts.append("[bgmraw][vsc]sidechaincompress=threshold=0.04:ratio=5:attack=30:release=500[bgm]")
            else:
                audio_parts.append(chain + "[bgm]")
            mix_labels.append("[bgm]")
            idx += 1

        sfx_used: list[dict] = []
        if self.audio_cfg.sfx_enabled:
            timing_map = {t.scene_number: t for t in timings}
            for scene in scenes:
                if len(sfx_used) >= self.audio_cfg.max_sfx or not scene.sfx:
                    continue
                path = self.library.sfx(scene.sfx)
                if not path:
                    continue
                at = timing_map[scene.scene_number].start
                ms = int(at * 1000)
                inputs += ["-i", str(path)]
                label = f"[sfx{idx}]"
                audio_parts.append(f"[{idx}:a]{fmt},volume={self.audio_cfg.sfx_volume},adelay={ms}:all=1{label}")
                mix_labels.append(label)
                sfx_used.append({"scene": scene.scene_number, "sfx": scene.sfx, "at": at})
                idx += 1

        post = f",loudnorm=I={self.audio_cfg.target_lufs}:TP=-1.5:LRA=11" if self.audio_cfg.loudnorm else ""
        audio_parts.append(
            "".join(mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0:normalize=0{post},aresample={sr}[a]"
        )

        g = self.cfg.global_fade
        video_part = (
            "[2:v]format=rgba,setpts=PTS-STARTPTS[subs];"
            "[0:v][subs]overlay=0:0:format=auto:eof_action=repeat,"
            f"fade=t=in:st=0:d={g},fade=t=out:st={max(total - g, 0):.3f}:d={g},"
            f"fps={self.cfg.fps},format=yuv420p[v]"
        )
        graph = video_part + ";" + ";".join(audio_parts)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        run_ffmpeg(
            [
                *inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", self.cfg.preset, "-crf", str(self.cfg.crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(self.cfg.fps),
                "-c:a", "aac", "-b:a", self.cfg.audio_bitrate, "-ar", str(sr), "-ac", "2",
                "-t", f"{total:.3f}", "-movflags", "+faststart", str(out_path),
            ],
            step="final render",
        )
        logger.info("Rendering completed: %s (%.1f초)", out_path.name, total)

        thumb = None
        if thumbnail_path:
            try:
                run_ffmpeg(["-ss", "1.2", "-i", str(out_path), "-frames:v", "1", "-q:v", "3", str(thumbnail_path)], step="thumbnail")
                thumb = thumbnail_path
            except Exception as exc:
                logger.warning("썸네일 생성 실패: %s", exc)
        return RenderResult(out_path, total, thumb, sfx_used, self.hook_effect)
