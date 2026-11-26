"""Frozen language-model prices for durable experiment paid-call admissions.

Rates are explicit inputs, never copied from the Decisions endpoint or inferred
from a subscription's token accounting. Costs are estimates unless the provider
independently reports a billed amount. All token counts use the inclusive SDK
input-token convention.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from civ_agent.decisions_ledger import canonical, fingerprint
from civ_agent.protocol import JsonObject


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 100_000_000 else None


@dataclass(frozen=True)
class LanguageModelPricing:
    provider: str
    model: str
    source_url: str
    observed_at: str
    input_nanodollars: int
    output_nanodollars: int
    read_nanodollars: int | None = None
    write_5m_nanodollars: int | None = None
    write_1h_nanodollars: int | None = None
    storage_nanodollars_per_token_hour: int | None = None
    maximum_storage_token_hours: int = 0

    def __post_init__(self):
        if not all((self.provider, self.model, self.source_url, self.observed_at)):
            raise ValueError("Frozen price provenance required")
        rates = list(asdict(self).values())[4:]
        if any(rate is not None and (type(rate) is not int or rate < 0) for rate in rates):
            raise ValueError("Prices must be nonnegative integer nanodollars")
        if self.storage_nanodollars_per_token_hour and not self.maximum_storage_token_hours:
            raise ValueError("Explicit cache storage requires a reservation bound")

    @property
    def rate_version(self) -> str:
        return "language-model-v1-" + fingerprint(asdict(self))[:32]

    def provenance(self) -> JsonObject:
        return {"kind": "estimated_language_model_cost", "rate_version": self.rate_version, **asdict(self)}

    def reservation(self, body: JsonObject) -> int:
        if body.get("model") != self.model:
            raise ValueError("Paid call frozen model conflict")
        output = _count(body.get("max_output_tokens"))
        if output is None:
            raise ValueError("Paid language-model calls require a bounded output")
        # A byte-per-token upper estimate includes serialized schemas/framing.
        # Cache benefits cannot reduce admission; write premiums can increase it.
        input_tokens = len(canonical(body).encode()) + 2048
        input_rate = max(
            self.input_nanodollars,
            self.read_nanodollars or 0,
            self.write_5m_nanodollars or 0,
            self.write_1h_nanodollars or 0,
        )
        storage = self.maximum_storage_token_hours * (self.storage_nanodollars_per_token_hour or 0)
        return input_tokens * input_rate + output * self.output_nanodollars + storage

    def charge(self, result: JsonObject) -> tuple[int, int] | None:
        usage = result.get("usage")
        if not isinstance(usage, dict):
            return None
        total, output, reads, writes = (
            _count(usage.get(name))
            for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        )
        if total is None or output is None or reads is None or writes is None or reads + writes > total:
            return None
        cost = (total - reads - writes) * self.input_nanodollars + output * self.output_nanodollars
        if reads:
            if self.read_nanodollars is None:
                return None
            cost += reads * self.read_nanodollars
        if writes:
            one_hour = _count(usage.get("cache_write_1h_tokens"))
            five_minute = _count(usage.get("cache_write_5m_tokens"))
            if one_hour is None or five_minute is None or one_hour + five_minute != writes:
                return None
            if five_minute and self.write_5m_nanodollars is None or one_hour and self.write_1h_nanodollars is None:
                return None
            cost += five_minute * (self.write_5m_nanodollars or 0) + one_hour * (self.write_1h_nanodollars or 0)
        token_hours = usage.get("explicit_cache_token_hours")
        if self.storage_nanodollars_per_token_hour is not None:
            # A configured explicit-cache route requires a measured allocation.
            # Zero must be supplied explicitly when no allocation was charged.
            try:
                duration = Decimal(str(token_hours))
            except (ValueError, ArithmeticError):
                return None
            if not duration.is_finite() or duration < 0:
                return None
            cost += int((duration * self.storage_nanodollars_per_token_hour).to_integral_value(rounding=ROUND_CEILING))
        elif token_hours is not None and token_hours != 0:
            return None
        return cost, total
