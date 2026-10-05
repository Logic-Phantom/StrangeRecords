"""모든 Gemini 호출은 이 GeminiClient 를 통해 수행한다."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from app.ai.json_parser import JSONParseError, parse_json
from app.config.settings import GeminiConfig, RetryConfig
from app.utils.logger import get_logger
from app.utils.retry import NonRetryableError, retry_call

logger = get_logger("gemini")

M = TypeVar("M", bound=BaseModel)

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

        def _call() -> str:
            self.call_count += 1
            try:
                response = self._client.models.generate_content(
                    model=self.config.model, contents=prompt, config=gen_config
                )
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

        return retry_call(
            _call,
            step="Gemini API",
            max_attempts=self.config.max_retries,
            delays=self.retry.delays,
            retry_on=(Exception,),
        )

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
    def generate_image(self, prompt: str, model: str) -> bytes:
        """Gemini 이미지 모델로 이미지를 생성한다 (config.assets.ai_image.enabled 일 때만 사용)."""
        types = self._types

        def _call() -> bytes:
            self.call_count += 1
            response = self._client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
            )
            for candidate in response.candidates or []:
                for part in candidate.content.parts or []:
                    inline = getattr(part, "inline_data", None)
                    if inline and inline.data:
                        return inline.data
            raise GeminiError("이미지 응답 없음")

        return retry_call(_call, step="Gemini Image", max_attempts=2, delays=self.retry.delays)
