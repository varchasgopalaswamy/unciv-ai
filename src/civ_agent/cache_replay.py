"""Offline estimates from observed gaps and frozen cache prices, never live billing."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class CacheTier(ReplayModel):
    name: str = Field(min_length=1)
    retention_seconds: Annotated[int, Field(gt=0)]
    input_per_million: Annotated[Decimal, Field(ge=Decimal(0), allow_inf_nan=False)]
    read_per_million: Annotated[Decimal, Field(ge=Decimal(0), allow_inf_nan=False)]
    write_per_million: Annotated[Decimal, Field(ge=Decimal(0), allow_inf_nan=False)]
    storage_per_million_token_hours: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] = Decimal(0)
    refresh_on_read: bool = True
    provenance: str = Field(min_length=1)


class CacheBreakpoint(ReplayModel):
    # Fingerprints represent the cumulative bytes through each breakpoint.
    fingerprint: str = Field(min_length=1, max_length=128)
    tokens: Annotated[int, Field(gt=0)]


class CacheObservation(ReplayModel):
    timestamp: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    family: str = Field(min_length=1)
    input_tokens: Annotated[int, Field(ge=0)]
    breakpoints: Annotated[tuple[CacheBreakpoint, ...], Field(max_length=4)] = ()

    @model_validator(mode="after")
    def cumulative_breakpoints(self):
        positions = [point.tokens for point in self.breakpoints]
        if positions != sorted(set(positions)) or positions and positions[-1] > self.input_tokens:
            raise ValueError("CACHE_BREAKPOINT_ORDER_INVALID")
        return self


@dataclass(frozen=True)
class CacheEstimate:
    tier: str
    requests: int
    input_tokens: int
    read_tokens: int
    write_tokens: int
    uncached_tokens: int
    storage_token_hours: Decimal
    estimated_usd: Decimal
    provenance: str


def replay_cache(
    observations: list[CacheObservation], tier: CacheTier, *, end_timestamp: float | None = None
) -> CacheEstimate:
    """Refresh reads, charge write premiums, and count overlapping prefixes once.

    Fingerprints and token lengths must come from the same prepared-prefix
    capture. Missing provider measurements cannot be filled in by this model.
    Storage runs through the selected horizon, defaulting to final expiration.
    """
    ordered = sorted(observations, key=lambda event: event.timestamp)
    if observations != ordered:
        raise ValueError("CACHE_OBSERVATIONS_OUT_OF_ORDER")
    if end_timestamp is not None and ordered and end_timestamp < ordered[-1].timestamp:
        raise ValueError("CACHE_REPLAY_HORIZON_PRECEDES_REQUEST")
    expires: dict[tuple[str, ...], float] = {}
    lengths: dict[tuple[str, ...], int] = {}
    reads = writes = uncached = inputs = 0
    storage = Decimal(0)
    previous = ordered[0].timestamp if ordered else 0

    def storage_tokens() -> int:
        nodes = {key[:i] for key in expires for i in range(2, len(key) + 1)}
        return sum(lengths[key] - lengths.get(key[:-1], 0) for key in nodes)

    def advance(now: float):
        nonlocal previous, storage
        # Integrate storage at every expiry, including expiries between calls.
        for boundary in sorted({expiry for expiry in expires.values() if previous < expiry <= now} | {now}):
            seconds = Decimal(str(boundary)) - Decimal(str(previous))
            storage += Decimal(storage_tokens()) * seconds / Decimal(3600)
            previous = boundary
            for key, expiry in list(expires.items()):
                if expiry <= boundary:
                    expires.pop(key)

    for event in ordered:
        advance(event.timestamp)
        inputs += event.input_tokens
        paths = [
            (event.family, *(point.fingerprint for point in event.breakpoints[: i + 1]))
            for i in range(len(event.breakpoints))
        ]
        hit = max((i for i, path in enumerate(paths) if path in expires), default=-1)
        read_tokens = event.breakpoints[hit].tokens if hit >= 0 else 0
        write_tokens = event.breakpoints[-1].tokens - read_tokens if paths else 0
        reads += read_tokens
        writes += write_tokens
        uncached += event.input_tokens - read_tokens - write_tokens
        for index, path in enumerate(paths):
            point = event.breakpoints[index]
            if path in lengths and lengths[path] != point.tokens:
                raise ValueError("CACHE_PREFIX_TOKEN_LENGTH_CHANGED")
            lengths[path] = point.tokens
            if index > hit or index == hit and tier.refresh_on_read:
                expires[path] = event.timestamp + tier.retention_seconds
    horizon = end_timestamp if end_timestamp is not None else max(expires.values(), default=previous)
    advance(horizon)
    million = Decimal(1000000)
    cost = (
        Decimal(reads) * tier.read_per_million
        + Decimal(writes) * tier.write_per_million
        + Decimal(uncached) * tier.input_per_million
        + storage * tier.storage_per_million_token_hours
    ) / million
    return CacheEstimate(tier.name, len(ordered), inputs, reads, writes, uncached, storage, cost, tier.provenance)


def select_cache_tier(estimates: list[CacheEstimate], tiers: list[CacheTier]) -> CacheEstimate:
    """Use a longer retention only for estimated savings strictly above ten percent."""
    if not estimates or {value.tier for value in estimates} != {tier.name for tier in tiers}:
        raise ValueError("CACHE_TIER_ESTIMATES_REQUIRED")
    duration = {tier.name: tier.retention_seconds for tier in tiers}
    shortest = min(estimates, key=lambda estimate: duration[estimate.tier])
    best = min(estimates, key=lambda estimate: estimate.estimated_usd)
    return best if best.estimated_usd < shortest.estimated_usd * Decimal("0.9") else shortest
