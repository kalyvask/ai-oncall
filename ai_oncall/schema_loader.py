"""Loads JSON Schemas from schemas/ and validates payloads against them.

The JSON Schemas are the wire contract; Pydantic models are an in-process
mirror. Both are checked against every fixture in tests/contracts/.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict[str, Any]:
    path = SCHEMA_DIR / f"{name}.json"
    with path.open(encoding="utf-8") as f:
        schema: dict[str, Any] = json.load(f)
    return schema


@lru_cache(maxsize=1)
def _registry() -> Registry:
    registry: Registry = Registry()
    for path in SCHEMA_DIR.glob("*.json"):
        with path.open(encoding="utf-8") as f:
            schema = json.load(f)
        resource = Resource(contents=schema, specification=DRAFT202012)
        if "$id" in schema:
            registry = registry.with_resource(uri=schema["$id"], resource=resource)
        registry = registry.with_resource(uri=path.name, resource=resource)
    return registry


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name), registry=_registry())


def validate(name: str, payload: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if payload does not match `name`."""
    validator_for(name).validate(payload)
