"""Pydantic models for Elasticsearch API responses."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Shards(BaseModel):
    """Shard progress information."""

    total: int = 0
    successful: int = 0
    skipped: int = 0
    failed: int = 0


class HitsContainer(BaseModel):
    """Container for search hits."""

    hits: list[dict[str, Any]] = Field(default_factory=list)
    total: dict[str, Any] | int = 0


class ResponseBody(BaseModel):
    """Inner response body from async search."""

    model_config = ConfigDict(populate_by_name=True)

    shards: Shards = Field(default_factory=Shards, alias="_shards")
    hits: HitsContainer = Field(default_factory=HitsContainer)
    took: int = 0


class AsyncSearchResponse(BaseModel):
    """Response from async search API."""

    id: str | None = None
    is_running: bool = False
    is_partial: bool = False
    response: ResponseBody = Field(default_factory=ResponseBody)

    @property
    def hits(self) -> list[dict[str, Any]]:
        """Get the list of hit documents."""
        return self.response.hits.hits

    @property
    def total_hits(self) -> int:
        """Get the number of hits returned."""
        return len(self.hits)


class PITResponse(BaseModel):
    """Response from Point-in-Time open API."""

    id: str


class SearchResponse(BaseModel):
    """Response from regular search API (used with PIT pagination)."""

    hits: HitsContainer = Field(default_factory=HitsContainer)
    pit_id: str | None = None

    @property
    def hit_list(self) -> list[dict[str, Any]]:
        """Get the list of hit documents."""
        return self.hits.hits
