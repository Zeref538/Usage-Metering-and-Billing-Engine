"""Money math. Pure functions, no database, all integers.

Units: micro-dollars (1,000,000 = $1.00). Token prices are per 1,000,000 tokens.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, StrictInt, model_validator

MTOK = 1_000_000
PRICING_FILE = Path(__file__).resolve().parent.parent / "config" / "pricing.json"


class Plan(BaseModel):
    name: str
    api_calls: StrictInt = Field(ge=0)
    ai_tokens: StrictInt = Field(ge=0)
    monthly_fee_cents: StrictInt = Field(ge=0)


class Pricing(BaseModel):
    """StrictInt refuses 0.3 or "300000": a float price is the bug this file exists to prevent."""
    currency: str
    unit: str
    api_call_micros: StrictInt = Field(ge=0)
    input_per_mtok_micros: StrictInt = Field(ge=0)
    cached_input_per_mtok_micros: StrictInt = Field(ge=0)
    output_per_mtok_micros: StrictInt = Field(ge=0)
    plans: dict[str, Plan]

    @model_validator(mode="after")
    def sane(self):
        if self.cached_input_per_mtok_micros > self.input_per_mtok_micros:
            raise ValueError("cached input must not cost more than fresh input")
        if not {"free", "pro"} <= self.plans.keys():
            raise ValueError("plans must include free and pro")
        return self


def load_pricing(path: Path = PRICING_FILE) -> Pricing:
    return Pricing.model_validate(json.loads(path.read_text(encoding="utf-8")))


PRICING = load_pricing()


@dataclass(frozen=True)
class Tokens:
    """Token counts the way providers report them, which is why they can't just be summed.

    input         every input token, the cached ones included
    cached_input  the part of `input` served from cache: a subset, not extra tokens
    output        visible output
    reasoning     hidden thinking tokens, billed at the output price
    """
    input: int = 0
    cached_input: int = 0
    output: int = 0
    reasoning: int = 0

    def __post_init__(self):
        if min(self.input, self.cached_input, self.output, self.reasoning) < 0:
            raise ValueError("token counts cannot be negative")
        if self.cached_input > self.input:
            raise ValueError("cached_input_tokens is part of input_tokens, so it cannot be larger")

    @property
    def quota_units(self) -> int:
        """What counts against the token quota. Cached tokens are already inside `input`."""
        return self.input + self.output + self.reasoning


def round_half_up(numerator: int, denominator: int) -> int:
    return (numerator + denominator // 2) // denominator


def token_numerator(t: Tokens, p: Pricing = PRICING) -> int:
    """Exact cost in micro-dollars x 1,000,000. Kept unrounded so a month is rounded once."""
    fresh_input = t.input - t.cached_input
    return (fresh_input * p.input_per_mtok_micros
            + t.cached_input * p.cached_input_per_mtok_micros
            + (t.output + t.reasoning) * p.output_per_mtok_micros)


def token_cost_micros(t: Tokens, p: Pricing = PRICING) -> int:
    return round_half_up(token_numerator(t, p), MTOK)


def request_cost_micros(t: Tokens, api_calls: int = 1, p: Pricing = PRICING) -> int:
    return api_calls * p.api_call_micros + token_cost_micros(t, p)


def usd(micros: int) -> str:
    """12100 -> "0.012100". String, so JSON never turns it into a float."""
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"
