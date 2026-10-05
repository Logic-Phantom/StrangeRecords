"""Pillow 로 Shorts 자막(외곽선 + 핵심 단어 강조)을 투명 PNG 로 그리고,
FFmpeg concat 목록으로 만들어 하나의 오버레이 스트림으로 합성한다.

libass/폰트 설정에 의존하지 않으므로 Windows/macOS 어디서나 같은 결과가 나온다.
"""

from __future__ import annotations

import platform
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.config.settings import SubtitleConfig, VideoConfig
from app.schemas import SubtitleChunk
from app.utils.logger import get_logger
from app.video.ffmpeg import concat_list_path

logger = get_logger("subtitle_renderer")

FONT_CANDIDATES = {
    "Windows": ["C:/Windows/Fonts/malgunbd.ttf", "C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/gulim.ttc"],
    "Darwin": [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/Library/Fonts/NanumGothicBold.ttf",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
    ],
    "Linux": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    ],
}


def find_font(config: SubtitleConfig, fonts_dir: Path) -> str:
    if config.font_path and Path(config.font_path).exists():
        return config.font_path
    for pattern in ("*.ttf", "*.otf", "*.ttc"):
        found = sorted(fonts_dir.glob(pattern))
        if found:
            return str(found[0])
    for candidate in FONT_CANDIDATES.get(platform.system(), []) + sum(FONT_CANDIDATES.values(), []):
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("한글 폰트를 찾을 수 없습니다. assets/fonts 에 .ttf 를 넣거나 subtitle.font_path 를 설정하세요.")


@lru_cache(maxsize=16)
def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    # AppleSDGothicNeo.ttc 는 index 에 따라 굵기가 다르다 (Bold 계열 사용)
    if path.endswith("AppleSDGothicNeo.ttc"):
        for index in (6, 8, 4, 0):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    return ImageFont.truetype(path, size)


def is_highlight(word: str, tokens: set[str]) -> bool:
    """강조 단어 판정. 한 글자 강조어는 정확히 같은 어절일 때만 (예: '한' 이 '한가운데서' 에 걸리지 않도록)."""
    bare = word.strip(".,!?…\"'")
    return any(t == bare or (len(t) >= 2 and t in bare) for t in tokens)


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


class SubtitleRenderer:
    def __init__(self, sub_cfg: SubtitleConfig, video_cfg: VideoConfig, fonts_dir: Path):
        self.cfg = sub_cfg
        self.size = (video_cfg.width, video_cfg.height)
        self.font_path = find_font(sub_cfg, fonts_dir)

    # ------------------------------------------------------------ drawing
    def _wrap(self, words: list[str], font: ImageFont.FreeTypeFont, max_width: int) -> list[list[str]]:
        lines: list[list[str]] = [[]]
        for word in words:
            trial = " ".join(lines[-1] + [word])
            if lines[-1] and font.getlength(trial) > max_width:
                lines.append([word])
            else:
                lines[-1].append(word)
        return lines

    def _draw_block(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        center_y: int,
        font_size: int,
        highlights: list[str] | None = None,
        color: str | None = None,
    ) -> None:
        w, _ = self.size
        font = _font(self.font_path, font_size)
        stroke = self.cfg.stroke_width
        max_width = int(w * 0.86)
        while font_size > 40 and max(font.getlength(word) for word in text.split()) > max_width:
            font_size -= 6
            font = _font(self.font_path, font_size)
        lines = self._wrap(text.split(), font, max_width)
        ascent, descent = font.getmetrics()
        line_h = int((ascent + descent) * 1.12)
        top = center_y - (line_h * len(lines)) // 2
        base_color = _hex(color or self.cfg.color)
        hl_color = _hex(self.cfg.highlight_color)
        stroke_color = _hex(self.cfg.stroke_color)
        space = font.getlength(" ")
        tokens = {t for h in highlights or [] for t in h.split()}

        for i, line_words in enumerate(lines):
            line_text = " ".join(line_words)
            x = (w - font.getlength(line_text)) / 2
            y = top + i * line_h
            for word in line_words:
                fill = hl_color if is_highlight(word, tokens) else base_color
                draw.text((x, y), word, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_color)
                x += font.getlength(word) + space

    def _base_frame(self, title: str | None) -> Image.Image:
        img = Image.new("RGBA", self.size, (0, 0, 0, 0))
        if title and self.cfg.show_title:
            draw = ImageDraw.Draw(img)
            self._draw_block(draw, title, int(self.size[1] * self.cfg.title_position_y), self.cfg.title_font_size,
                             color=self.cfg.highlight_color)
        return img

    def render_chunk(self, chunk: SubtitleChunk, path: Path, title: str | None = None) -> Path:
        img = self._base_frame(title)
        draw = ImageDraw.Draw(img)
        self._draw_block(draw, chunk.text, int(self.size[1] * self.cfg.position_y), self.cfg.font_size, chunk.highlights)
        img.save(path, optimize=True)
        return path

    # -------------------------------------------------------------- track
    def build_track(self, chunks: list[SubtitleChunk], total_duration: float, work_dir: Path, title: str | None = None) -> Path:
        """자막 PNG 시퀀스 + 빈 프레임으로 concat 목록(ffconcat)을 만든다."""
        work_dir.mkdir(parents=True, exist_ok=True)
        blank = work_dir / "blank.png"
        self._base_frame(title).save(blank, optimize=True)

        entries: list[tuple[Path, float]] = []
        cursor = 0.0
        for chunk in chunks:
            start, end = max(chunk.start, cursor), min(chunk.end, total_duration)
            if end <= start:
                continue
            if start - cursor > 0.001:
                entries.append((blank, start - cursor))
            png = self.render_chunk(chunk, work_dir / f"sub_{chunk.index:03d}.png", title)
            entries.append((png, end - start))
            cursor = end
        if total_duration - cursor > 0.001:
            entries.append((blank, total_duration - cursor))

        list_file = work_dir / "subtitles.ffconcat"
        lines = ["ffconcat version 1.0"]
        for path, duration in entries:
            lines.append(f"file '{concat_list_path(path)}'")
            lines.append(f"duration {duration:.3f}")
        lines.append(f"file '{concat_list_path(entries[-1][0])}'")  # 마지막 duration 적용을 위한 반복
        list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("자막 이미지 %d개 생성 (font: %s)", len(chunks), Path(self.font_path).name)
        return list_file
