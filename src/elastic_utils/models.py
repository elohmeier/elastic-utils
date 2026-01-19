"""Pydantic models for Elasticsearch API responses.

Models are validated against the elasticsearch-specification:
https://github.com/elastic/elasticsearch-specification
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --- Shard Statistics (from _types/Stats.ts) ---


class Shards(BaseModel):
    """Shard statistics from ES responses.

    Spec: ShardStatistics in _types/Stats.ts
    """

    total: int
    successful: int
    failed: int
    skipped: int = 0  # optional per spec


# --- Search Response Models ---


class TotalHits(BaseModel):
    """Total hits information.

    Spec: TotalHits in _global/search/_types/hits.ts
    """

    value: int
    relation: str  # "eq" or "gte"


class HitsContainer(BaseModel):
    """Container for search hits.

    Spec: HitsMetadata in _global/search/_types/hits.ts
    """

    hits: list[dict[str, Any]] = Field(default_factory=list)
    total: TotalHits | int | None = None  # optional per spec

    @property
    def total_count(self) -> int:
        """Get the total document count."""
        if isinstance(self.total, TotalHits):
            return self.total.value
        if isinstance(self.total, int):
            return self.total
        return 0


class ResponseBody(BaseModel):
    """Inner response body from async search.

    Spec: AsyncSearch in async_search/_types/AsyncSearch.ts
    """

    model_config = ConfigDict(populate_by_name=True)

    shards: Shards = Field(alias="_shards")
    hits: HitsContainer
    took: int
    timed_out: bool


class AsyncSearchResponse(BaseModel):
    """Response from async search API.

    Spec: AsyncSearchDocumentResponseBase in async_search/_types/AsyncSearchResponseBase.ts
    """

    id: str | None = None  # optional per spec
    is_running: bool
    is_partial: bool
    response: ResponseBody

    @property
    def hits(self) -> list[dict[str, Any]]:
        """Get the list of hit documents."""
        return self.response.hits.hits

    @property
    def total_hits(self) -> int:
        """Get the total number of matching documents."""
        return self.response.hits.total_count


class PITResponse(BaseModel):
    """Response from Point-in-Time open API.

    Spec: OpenPointInTimeResponse in _global/open_point_in_time/OpenPointInTimeResponse.ts
    """

    id: str


class SearchResponse(BaseModel):
    """Response from regular search API (used with PIT pagination).

    Spec: ResponseBody in _global/search/SearchResponse.ts
    """

    model_config = ConfigDict(populate_by_name=True)

    hits: HitsContainer
    pit_id: str | None = None  # optional per spec

    @property
    def hit_list(self) -> list[dict[str, Any]]:
        """Get the list of hit documents."""
        return self.hits.hits


# --- Catalog Models (for _cat APIs) ---
# Spec: cat/indices/types.ts, cat/aliases/types.ts
# Note: _cat APIs return all fields as strings


class IndexInfo(BaseModel):
    """Response from _cat/indices API.

    Spec: IndicesRecord in cat/indices/types.ts
    All fields are strings per _cat API format.
    """

    model_config = ConfigDict(populate_by_name=True)

    index: str | None = None  # optional per spec
    health: str | None = None
    status: str | None = None
    pri: str | None = None  # string per spec
    rep: str | None = None  # string per spec
    docs_count: str | None = Field(None, alias="docs.count")  # string|null per spec
    store_size: str | None = Field(None, alias="store.size")  # string|null per spec
    creation_date: str | None = Field(None, alias="creation.date")


class AliasInfo(BaseModel):
    """Response from _cat/aliases API.

    Spec: AliasesRecord in cat/aliases/types.ts
    """

    model_config = ConfigDict(populate_by_name=True)

    alias: str | None = None  # optional per spec
    index: str | None = None  # optional per spec
    filter: str | None = None
    routing_index: str | None = Field(None, alias="routing.index")
    routing_search: str | None = Field(None, alias="routing.search")
    is_write_index: str | None = None


# --- Auth Models ---


class ApiKeyResponse(BaseModel):
    """Response from API key creation.

    Spec: SecurityCreateApiKeyResponse in security/create_api_key/SecurityCreateApiKeyResponse.ts
    """

    api_key: str
    id: str
    name: str
    encoded: str
    expiration: int | None = None  # optional per spec (milliseconds)


# --- Cluster Info Models ---


class ClusterVersion(BaseModel):
    """Cluster version info.

    Spec: ElasticsearchVersionInfo in _types/Base.ts
    """

    number: str
    build_flavor: str
    build_type: str
    build_hash: str
    build_date: str
    build_snapshot: bool
    lucene_version: str
    minimum_index_compatibility_version: str
    minimum_wire_compatibility_version: str


class ClusterInfo(BaseModel):
    """Response from cluster info endpoint.

    Spec: RootNodeInfoResponse in _global/info/RootNodeInfoResponse.ts
    """

    name: str
    cluster_name: str
    cluster_uuid: str
    version: ClusterVersion
    tagline: str


# --- ILM Models ---


class ILMIndexInfo(BaseModel):
    """ILM info for a single index.

    Spec: LifecycleExplain (union of Managed/Unmanaged) in ilm/explain_lifecycle/types.ts

    For managed indices (managed=true): phase, action, step, age etc. are available
    For unmanaged indices (managed=false): only index and managed are present
    """

    index: str
    managed: bool
    # Fields only present when managed=true
    policy: str | None = None
    phase: str | None = None
    action: str | None = None
    step: str | None = None
    age: str | None = None


class ILMExplainResponse(BaseModel):
    """Response from ILM explain API.

    Spec: ExplainLifecycleResponse in ilm/explain_lifecycle/ExplainLifecycleResponse.ts
    """

    indices: dict[str, ILMIndexInfo] = Field(default_factory=dict)


# --- Alias Detail Models ---


class AliasConfig(BaseModel):
    """Configuration for a single alias on an index.

    Spec: Alias in indices/_types/Alias.ts
    """

    filter: dict[str, Any] | None = None
    index_routing: str | None = None
    search_routing: str | None = None
    is_write_index: bool | None = None


class IndexAliases(BaseModel):
    """Aliases configured on an index."""

    aliases: dict[str, AliasConfig] = Field(default_factory=dict)


# --- Date Range Aggregation Models ---


class DateAggValue(BaseModel):
    """Single date aggregation value (min/max aggregation result).

    Spec: MinAggregate/MaxAggregate in _types/aggregations/Aggregate.ts
    """

    value: float | None = None  # can be null if no docs
    value_as_string: str | None = None


class DateRangeAggregations(BaseModel):
    """Aggregations for date range query."""

    min_date: DateAggValue = Field(default_factory=DateAggValue)
    max_date: DateAggValue = Field(default_factory=DateAggValue)


class DateRangeResponse(BaseModel):
    """Response from date range aggregation search."""

    aggregations: DateRangeAggregations = Field(default_factory=DateRangeAggregations)
