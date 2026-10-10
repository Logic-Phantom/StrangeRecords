"""모든 Gemini 호출은 이 GeminiClient 를 통해 수행한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.json_parser import JSONParseError, parse_json
from app.config.settings import GeminiConfig, RetryConfig
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError, RetryError, retry_call

logger = get_logger("gemini")

M = TypeVar("M", bound=BaseModel)

# 다른 모델로 전환할 오류: 한도 초과(429), 서버 오류/과부하(500/503/504)
FALLBACK_CODES = {429, 500, 503, 504}

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


class GeminiError(Exception):
    pass


class ContentValidationError(ValueError):
    """JSON 은 정상이지만 내용 규칙(길이, 필수 항목 등)을 어긴 경우. 피드백과 함께 재요청한다."""


def load_prompt(name: str, **variables: Any) -> str:
    """prompts/<name>.txt 를 읽어 {{변수}} 를 치환한다 (JSON 예시의 중괄호와 충돌하지 않음)."""
    template = (PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")
    for key, value in variables.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template


class GeminiClient:
    def __init__(self, config: GeminiConfig, api_key: str, retry: RetryConfig | None = None):
        if not api_key:
            raise NonRetryableError("GEMINI_API_KEY 가 설정되지 않았습니다 (.env 확인)")
        from google import genai
        from google.genai import types

        self._types = types
        self.config = config
        self.retry = retry or RetryConfig()
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=config.request_timeout * 1000),
        )
        self.call_count = 0
        self._active = 0

    # ------------------------------------------------------------------ text
    def generate(
        self,
        prompt: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        system_instruction: str | None = None,
        use_search: bool = False,
    ) -> str:
        types = self._types
        tools = [types.Tool(google_search=types.GoogleSearch())] if use_search else None
        gen_config = types.GenerateContentConfig(
            temperature=self.config.temperature if temperature is None else temperature,
            max_output_tokens=self.config.max_output_tokens,
            response_mime_type="application/json" if json_mode and not use_search else None,
            system_instruction=system_instruction,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        def _call(model: str) -> str:
            self.call_count += 1
            try:
                response = self._client.models.generate_content(model=model, contents=prompt, config=gen_config)
            except Exception as exc:
                code = getattr(exc, "code", None)
                # 잘못된 키/모델명/요청 형식은 재시도해도 같으므로 즉시 실패
                if code in (400, 401, 403, 404):
                    raise NonRetryableError(f"Gemini 요청 거부 ({code}): {exc}") from exc
                raise
            text = getattr(response, "text", None)
            if not text:
                reason = ""
                try:
                    reason = str(response.candidates[0].finish_reason)
                except (AttributeError, IndexError, TypeError):
                    reason = str(getattr(response, "prompt_feedback", ""))
                raise GeminiError(f"빈 응답 (finish_reason={reason})")
            return text

        # 기본 모델이 과부하(503)/한도 초과(429)로 계속 실패하면 config 의 fallback_models 순서로 전환
        models = self.models
        start = self._active
        for i in range(start, len(models)):
            model = models[i]
            try:
                text = retry_call(
                    lambda: _call(model),
                    step=f"Gemini API ({model})",
                    max_attempts=self.config.max_retries,
                    delays=self.retry.delays,
                    retry_on=(Exception,),
                )
                if i != self._active:
                    logger.warning("Gemini 모델 전환: %s → %s (이번 실행 동안 유지)", models[self._active], model)
                    self._active = i
                return text
            except RetryError as exc:
                code = getattr(exc.last_error, "code", None)
                if code in FALLBACK_CODES and i + 1 < len(models):
                    logger.warning("%s 사용 불가(%s) → 다음 모델 %s 시도", model, code, models[i + 1])
                    continue
                raise
        raise GeminiError("사용 가능한 Gemini 모델 없음")

    @property
    def models(self) -> list[str]:
        ordered: list[str] = []
        for name in [self.config.model, *self.config.fallback_models]:
            if name and name not in ordered:
                ordered.append(name)
        return ordered

    @property
    def active_model(self) -> str:
        return self.models[self._active]

    # ------------------------------------------------------------------ json
    def generate_json(
        self,
        prompt: str,
        model: type[M],
        *,
        validate: Callable[[M], None] | None = None,
        temperature: float | None = None,
        system_instruction: str | None = None,
    ) -> M:
        """JSON 응답을 pydantic 모델로 검증한다.

        파싱/검증 실패 → 1회 재요청 → 실패 → 2회 재요청 → 실패 → 예외 (무한 재시도 없음)
        """
        attempts = 1 + self.config.json_retries
        feedback = ""
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            full_prompt = prompt if not feedback else (
                f"{prompt}\n\n[이전 응답의 문제]\n{feedback}\n위 문제를 반드시 고쳐서 JSON 만 다시 출력하라."
            )
            text = self.generate(
                full_prompt, json_mode=True, temperature=temperature, system_instruction=system_instruction
            )
            try:
                data = parse_json(text)
                if isinstance(data, list) and "candidates" in model.model_fields:
                    data = {"candidates": data}
                elif isinstance(data, list) and "scenes" in model.model_fields:
                    data = {"scenes": data}
                result = model.model_validate(data)
                if validate:
                    validate(result)
                return result
            except (JSONParseError, ValidationError, ContentValidationError) as exc:
                last_error = exc
                feedback = str(exc)[:1500]
                logger.warning("Gemini JSON 응답 문제 (%d/%d): %s", attempt, attempts, feedback.splitlines()[0])
        raise GeminiError(f"Gemini JSON 응답 {attempts}회 실패: {last_error}")

    def list_models(self) -> list[str]:
        """현재 API Key 로 사용 가능한 텍스트 생성 모델 목록 (config 의 gemini.model 선택용)."""
        names = []
        for m in self._client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append(m.name.removeprefix("models/"))
        return names

    # ----------------------------------------------------------------- image
    def generate_image(self, prompt: str, models: str | list[str], aspect_ratio: str = "9:16") -> tuple[bytes, str, str]:
        """Gemini 이미지 모델로 이미지를 생성한다 (config.assets.ai_image.enabled 일 때만 사용).

        반환: (이미지 bytes, mime type, 사용한 모델). 모델이 없거나(404) 한도 초과/과부하면 다음 모델로 전환하고,
        모든 모델이 키/권한/한도 문제로 실패하면 NonRetryableError (이번 작업에서 AI 이미지 제외).
        """
        types = self._types
        models = [models] if isinstance(models, str) else [m for m in models if m]
        config_kwargs: dict[str, Any] = {"response_modalities": ["IMAGE", "TEXT"]}
        if aspect_ratio and hasattr(types, "ImageConfig"):
            config_kwargs["image_config"] = types.ImageConfig(aspect_ratio=aspect_ratio)
        gen_config = types.GenerateContentConfig(**config_kwargs)

        def _call(model: str) -> tuple[bytes, str]:
            self.call_count += 1
            try:
                response = self._client.models.generate_content(model=model, contents=prompt, config=gen_config)
            except Exception as exc:
                code = getattr(exc, "code", None)
                if code in (400, 401, 403, 404):
                    raise NonRetryableError(f"Gemini 이미지 요청 거부 ({code}): {exc}") from exc
                if code == 429 and "free_tier" in str(exc) and "limit: 0" in str(exc):
                    # 무료 티어에서는 이미지 모델 한도가 0 → 기다려도 풀리지 않음
                    raise NonRetryableError(
                        "Gemini 이미지 모델은 무료 티어 한도가 0 입니다. Google AI Studio(https://aistudio.google.com/)"
                        " 에서 이 API Key 의 프로젝트에 결제(Billing)를 연결하면 AI 장면 이미지가 생성됩니다."
                    ) from exc
                if code == 429:
                    # RESOURCE_EXHAUSTED: 기다리지 않고 다음 모델 → 모두 막히면 다음 provider(Hugging Face 등)
                    raise NonRetryableError(f"Gemini 이미지 한도 초과 (429): {exc}") from exc
                raise
            for candidate in response.candidates or []:
                for part in (candidate.content.parts if candidate.content else None) or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        return inline.data, inline.mime_type or "image/png"
            raise GeminiError("이미지 응답 없음 (안전 필터 등)")

        last: Exception | None = None
        for model in models:
            try:
                data, mime = retry_call(
                    lambda: _call(model), step=f"Gemini Image ({model})", max_attempts=2, delays=self.retry.delays,
                    retry_on=(Exception,),
                )
                return data, mime, model
            except NonRetryableError as exc:
                last = exc
                cause_code = getattr(exc.__cause__, "code", None)
                if cause_code == 404 or (cause_code == 429 and "limit: 0" not in str(exc.__cause__)):
                    logger.warning("이미지 모델 %s 사용 불가(%s) → 다음 모델", model, cause_code)
                    continue
                raise
            except RetryError as exc:
                last = exc
                code = getattr(exc.last_error, "code", None)
                if code in FALLBACK_CODES:
                    logger.warning("이미지 모델 %s 사용 불가(%s) → 다음 모델", model, code)
                    continue
                raise GeminiError(f"이미지 생성 실패: {exc.last_error}") from exc
        # 모든 모델이 한도 초과/없음 → 이번 작업 동안 AI 이미지를 건너뛰도록 NonRetryable
        raise NonRetryableError(
            f"사용 가능한 Gemini 이미지 모델 없음 ({', '.join(models)}). 무료 티어에서 이미지 생성이 막혀 있으면"
            f" Google AI Studio 에서 결제(Billing)를 연결해야 합니다: {last}"
        )
