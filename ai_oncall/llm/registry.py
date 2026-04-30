"""Model IDs in one place. The agent never hardcodes a model string elsewhere.

Default per BRIEF.md §9: claude-haiku. claude-opus is opt-in via AI_ONCALL_RCA_MODEL.
"""

from __future__ import annotations

from typing import TypedDict


class ModelSpec(TypedDict):
    provider: str
    id: str
    input_per_million: float
    output_per_million: float


CATALOG: dict[str, ModelSpec] = {
    "claude-haiku": {
        "provider": "anthropic",
        "id": "claude-haiku-4-5-20251001",
        "input_per_million": 1.0,
        "output_per_million": 5.0,
    },
    "claude-sonnet": {
        "provider": "anthropic",
        "id": "claude-sonnet-4-6",
        "input_per_million": 3.0,
        "output_per_million": 15.0,
    },
    "claude-opus": {
        "provider": "anthropic",
        "id": "claude-opus-4-7",
        "input_per_million": 15.0,
        "output_per_million": 75.0,
    },
    "mock": {
        "provider": "mock",
        "id": "mock-deterministic",
        "input_per_million": 0.0,
        "output_per_million": 0.0,
    },
}


def estimate_cost(alias: str, tokens_in: int, tokens_out: int) -> float:
    spec = CATALOG[alias]
    return (
        tokens_in * spec["input_per_million"] / 1_000_000
        + tokens_out * spec["output_per_million"] / 1_000_000
    )
