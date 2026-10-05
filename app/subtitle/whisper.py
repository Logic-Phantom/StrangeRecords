"""faster-whisper 로 실제 음성의 단어 단위 타이밍을 얻는다."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from app.config.settings import SubtitleConfig
from app.utils.logger import get_logger

logger = get_logger("whisper")


@dataclass
class Word:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


class WhisperTranscriber:
    def __init__(self, config: SubtitleConfig, model_dir: Path):
        self.config = config
        self.model_dir = model_dir
        self._model = None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            device = self.config.device
            if device == "auto":
                device = "cpu"
                try:
                    import ctranslate2

                    if ctranslate2.get_cuda_device_count() > 0:
                        device = "cuda"
                except Exception:
                    pass
            compute_type = self.config.compute_type if device == "cpu" else "float16"
            logger.info("Whisper 모델 로드: %s (%s, %s)", self.config.model_size, device, compute_type)
            self.model_dir.mkdir(parents=True, exist_ok=True)
            self._model = WhisperModel(
                self.config.model_size, device=device, compute_type=compute_type, download_root=str(self.model_dir)
            )
        return self._model

    def transcribe(self, audio_path: Path, language: str = "ko", initial_prompt: str | None = None) -> list[Word]:
        logger.info("Whisper started")
        model = self._load()
        segments, _info = model.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=False,
            beam_size=5,
            # 주의: 대본 첫 문장을 initial_prompt 로 주면 Whisper 가 그 문장을 이미 들은 것으로 보고 건너뛴다
            initial_prompt=initial_prompt[:200] if initial_prompt else None,
            condition_on_previous_text=False,
        )
        words: list[Word] = []
        for segment in segments:
            for w in segment.words or []:
                text = w.word.strip()
                if text:
                    words.append(Word(start=float(w.start), end=float(w.end), text=text))
        logger.info("Whisper completed: %d words", len(words))
        return words
