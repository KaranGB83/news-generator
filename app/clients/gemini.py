import asyncio
import json
import logging
import re
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# 429 covers both per-minute and per-day limits; per-minute ones clear within a retry or two.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# finishReason values meaning the model refused or was cut off by policy, not by length.
BLOCKED_FINISH_REASONS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
}

T = TypeVar("T", bound=BaseModel)


class GeminiError(Exception):
    """Base error for Gemini failures."""


class GeminiBlocked(GeminiError):
    """The prompt or the answer was blocked by Gemini's safety filters. Retrying won't help."""


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        backoff_seconds: float = 4.0,
        validation_retries: int = 1,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self._model = model or settings.gemini_model
        self._client = client or httpx.AsyncClient(base_url=BASE_URL, timeout=60.0)
        self._owns_client = client is None
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._validation_retries = validation_retries

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "GeminiClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def generate_json(
        self,
        prompt: str,
        response_model: type[T],
        *,
        system_instruction: str | None = None,
        temperature: float = 0.2,
        max_output_tokens: int = 8192,
    ) -> T:
        """Ask Gemini for JSON and return it validated as `response_model`.

        The prompt must describe the expected JSON shape. If the reply isn't valid JSON for the
        model, the call is repeated up to `validation_retries` more times.
        """
        last_error: Exception | None = None
        for attempt in range(self._validation_retries + 1):
            text = await self._generate_text(
                prompt,
                system_instruction=system_instruction,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
            )
            try:
                return response_model.model_validate_json(_strip_code_fence(text))
            except ValidationError as exc:
                last_error = exc
                logger.warning(
                    "Gemini reply failed validation (attempt %d/%d): %s",
                    attempt + 1,
                    self._validation_retries + 1,
                    exc.errors(include_url=False, include_input=False),
                )
        raise GeminiError(f"Gemini returned JSON that did not match the schema: {last_error}")

    async def _generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None,
        temperature: float,
        max_output_tokens: int,
    ) -> str:
        if not self._api_key:
            raise GeminiError("GEMINI_API_KEY is not set")

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": temperature,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        path = f"/models/{self._model}:generateContent"
        # Key goes in a header, not the query string, so it never lands in URL logs.
        headers = {"x-goog-api-key": self._api_key}

        last_error = "unknown error"
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(path, json=body, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc!r}"
            else:
                if response.status_code == 200:
                    return self._extract_text(response)
                last_error = f"HTTP {response.status_code}: {_error_text(response)}"
                if response.status_code not in RETRYABLE_STATUS:
                    # 400 (bad request / invalid key / unknown model), 403, 404: won't fix itself.
                    raise GeminiError(last_error)

            if attempt < self._max_retries:
                delay = self._backoff * (2**attempt)
                logger.warning("Gemini call failed (%s); retrying in %.1fs", last_error, delay)
                await asyncio.sleep(delay)

        raise GeminiError(f"Gemini failed after retries: {last_error}")

    @staticmethod
    def _extract_text(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError as exc:
            raise GeminiError("Gemini returned invalid JSON") from exc

        block_reason = (payload.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise GeminiBlocked(f"prompt blocked: {block_reason}")

        candidates = payload.get("candidates") or []
        if not candidates:
            raise GeminiError("Gemini returned no candidates")

        candidate = candidates[0]
        finish_reason = candidate.get("finishReason")
        if finish_reason in BLOCKED_FINISH_REASONS:
            raise GeminiBlocked(f"answer blocked: {finish_reason}")
        if finish_reason == "MAX_TOKENS":
            # Includes any hidden "thinking" tokens; a truncated JSON body is unusable.
            raise GeminiError("Gemini hit the output token limit before finishing")

        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise GeminiError(f"Gemini returned an empty answer (finishReason={finish_reason})")
        return text


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Models occasionally wrap JSON in ```json fences even in JSON mode."""
    match = _FENCE.match(text)
    return match.group(1) if match else text.strip()


def _error_text(response: httpx.Response) -> str:
    """Google errors look like {"error": {"code": 429, "message": "...", "status": "..."}}."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return response.text[:200]
    if isinstance(error, dict):
        status = error.get("status")
        message = error.get("message", "")
        return f"{status}: {message}" if status else str(message)
    return json.dumps(error)[:200] if error else response.text[:200]
