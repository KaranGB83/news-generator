from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class GNewsSource(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    name: str | None = None
    url: str | None = None
    country: str | None = None  # only present in search responses


class GNewsArticle(BaseModel):
    """One article from GNews. Free-plan `content` is truncated by the API."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    title: str
    description: str | None = None
    content: str | None = None
    url: str
    image: str | None = None
    published_at: datetime | None = Field(default=None, alias="publishedAt")
    lang: str | None = None
    source: GNewsSource = Field(default_factory=GNewsSource)
