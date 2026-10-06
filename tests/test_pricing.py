"""The token pricing rules, with totals worked out by hand from config/pricing.json:
input 0.30, cached input 0.075, output 2.50 dollars per million tokens; 200 micro-dollars per call."""
import json

import pytest
from pydantic import ValidationError

from app.pricing import PRICING, Pricing, Tokens, request_cost_micros, token_cost_micros, usd


def test_the_pinned_constants_are_the_ones_these_tests_assume():
    assert (PRICING.input_per_mtok_micros, PRICING.cached_input_per_mtok_micros,
            PRICING.output_per_mtok_micros, PRICING.api_call_micros) == (300_000, 75_000, 2_500_000, 200)


def test_worked_example_from_the_readme():
    # 10,000 input of which 4,000 cached, 1,500 output, 2,500 reasoning
    #   fresh input  6,000 x 0.30 / 1M = 1,800 micros
    #   cached       4,000 x 0.075/ 1M =   300 micros
    #   output+reasoning 4,000 x 2.50 / 1M = 10,000 micros
    t = Tokens(input=10_000, cached_input=4_000, output=1_500, reasoning=2_500)
    assert token_cost_micros(t) == 12_100
    assert request_cost_micros(t) == 12_300          # + one API call
    assert usd(12_300) == "0.012300"
    assert t.quota_units == 14_000                   # cached tokens are inside input, counted once


def test_cached_tokens_are_cheaper_than_fresh_ones():
    fresh = Tokens(input=1_000_000)
    cached = Tokens(input=1_000_000, cached_input=1_000_000)
    assert token_cost_micros(fresh) == 300_000
    assert token_cost_micros(cached) == 75_000


def test_reasoning_tokens_cost_the_same_as_output():
    assert token_cost_micros(Tokens(reasoning=1_000_000)) == token_cost_micros(Tokens(output=1_000_000)) == 2_500_000


def test_naive_sum_of_categories_gives_the_wrong_answer():
    # The bug the brief warns about: add all four counts and price them as input.
    t = Tokens(input=10_000, cached_input=4_000, output=1_500, reasoning=2_500)
    naive = (t.input + t.cached_input + t.output + t.reasoning) * 300_000 // 1_000_000
    assert naive == 5_400 and token_cost_micros(t) == 12_100


def test_rounding_is_half_up_on_whole_micro_dollars():
    assert token_cost_micros(Tokens(input=1)) == 0      # 0.3 micros
    assert token_cost_micros(Tokens(input=2)) == 1      # 0.6 micros
    assert token_cost_micros(Tokens(output=1)) == 3     # 2.5 micros, half rounds up


def test_cached_cannot_exceed_input():
    with pytest.raises(ValueError):
        Tokens(input=10, cached_input=11)


def test_a_float_price_in_config_is_refused():
    raw = json.loads(PRICING.model_dump_json())
    raw["input_per_mtok_micros"] = 0.3
    with pytest.raises(ValidationError):
        Pricing.model_validate(raw)


def test_cached_price_above_input_price_is_refused():
    raw = json.loads(PRICING.model_dump_json())
    raw["cached_input_per_mtok_micros"] = 400_000
    with pytest.raises(ValidationError):
        Pricing.model_validate(raw)
