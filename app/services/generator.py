import logging
import re
from dataclasses import dataclass
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.clients.gemini import GeminiBlocked
from app.models import Category
from app.prompts import (
    BODY_MIN_WORDS,
    CATEGORY_CONFIGS,
    SYSTEM_INSTRUCTION,
    TITLE_MAX_CHARS,
    CategoryConfig,
    build_prompt,
)
from app.schemas import GeneratedArticle, GNewsArticle

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Below this much source text there is not enough to write an accurate post.
MIN_SOURCE_CHARS = 120
BODY_MAX_WORDS = 600
# A little slack over the prompt's limit before an over-long title counts as a failure.
TITLE_HARD_MAX_CHARS = TITLE_MAX_CHARS + 30
GENERATION_ATTEMPTS = 2

# Free-plan GNews content ends like "… [1234 chars]".
_TRUNCATION_MARKER = re.compile(r"\s*(?:…|\.\.\.)?\s*\[\+?\d+\s*chars\]\s*$")
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")

_SOURCE_LABEL = {"en": "Source", "mr": "स्रोत"}


class JsonGenerator(Protocol):
    async def generate_json(
        self,
        prompt: str,
        response_model: type[T],
        *,
        system_instruction: str | None = ...,
        temperature: float = ...,
        max_output_tokens: int = ...,
    ) -> T: ...


@dataclass(frozen=True)
class GenerationOutcome:
    title: str = ""
    body: str = ""
    skip_reason: str | None = None

    @property
    def skipped(self) -> bool:
        return self.skip_reason is not None


async def generate_article(
    article: GNewsArticle,
    category: Category,
    gemini: JsonGenerator,
) -> GenerationOutcome:
    """Write a short original post for `article`, or explain why it was skipped.

    Skips (rather than raising) when the source is too thin, Gemini declines or is blocked by its
    safety filter, or the output stays unusable after retries, so the pipeline can try the next
    candidate. Other Gemini errors (quota, network) propagate so the caller can mark a failure.
    """
    config = CATEGORY_CONFIGS[category]
    if source_body_length(article) < MIN_SOURCE_CHARS:
        return GenerationOutcome(skip_reason="source text too short")

    prompt = build_prompt(config, article, build_source_text(article))
    problem = "no attempt made"
    for attempt in range(1, GENERATION_ATTEMPTS + 1):
        try:
            result = await gemini.generate_json(
                prompt,
                GeneratedArticle,
                system_instruction=SYSTEM_INSTRUCTION,
                temperature=0.3,
            )
        except GeminiBlocked as exc:
            return GenerationOutcome(skip_reason=f"blocked by Gemini: {exc}")

        if result.skip:
            return GenerationOutcome(skip_reason=result.skip_reason or "skipped by model")

        title, body = result.title.strip(), result.body.strip()
        problem = check_output(config, title, body)
        if problem is None:
            return GenerationOutcome(title=title, body=body)
        logger.warning("Generated article rejected (attempt %d): %s", attempt, problem)

    return GenerationOutcome(skip_reason=f"invalid output: {problem}")


def _clean(text: str | None) -> str:
    """Strip whitespace and GNews's "… [1234 chars]" truncation marker."""
    return _TRUNCATION_MARKER.sub("", (text or "").strip()).strip()


def source_body_length(article: GNewsArticle) -> int:
    """How much real text (beyond the headline) we have to write from."""
    description, content = _clean(article.description), _clean(article.content)
    return len(description) + (0 if content == description else len(content))


def build_source_text(article: GNewsArticle) -> str:
    """Headline, summary and cleaned content, without repeating identical text."""
    description, content = _clean(article.description), _clean(article.content)
    parts = [f"Headline: {article.title.strip()}"]
    if description:
        parts.append(f"Summary: {description}")
    if content and content != description:
        parts.append(f"Text: {content}")
    return "\n".join(parts)


def check_output(config: CategoryConfig, title: str, body: str) -> str | None:
    """Return a description of what is wrong with the output, or None if it is usable."""
    if not title:
        return "empty title"
    if len(title) > TITLE_HARD_MAX_CHARS:
        return f"title is {len(title)} characters"
    words = len(body.split())
    if words < BODY_MIN_WORDS:
        return f"body has only {words} words"
    if words > BODY_MAX_WORDS:
        return f"body has {words} words"

    devanagari = len(_DEVANAGARI.findall(title + body))
    latin = len(_LATIN.findall(title + body))
    total = devanagari + latin
    share = devanagari / total if total else 0.0
    if config.language_code == "mr" and share < 0.6:
        return "text is not mostly Marathi (Devanagari)"
    if config.language_code == "en" and share > 0.1:
        return "text is not English"
    return None


def with_attribution(body: str, source_name: str | None, source_url: str, language_code: str) -> str:
    """Append a source line so every post credits and links the original reporting."""
    label = _SOURCE_LABEL.get(language_code, _SOURCE_LABEL["en"])
    name = (source_name or "").strip()
    credit = f"{name} ({source_url})" if name else source_url
    return f"{body}\n\n{label}: {credit}"
