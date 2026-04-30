"""Telemetry store drivers (BRIEF.md §9).

Selected at runtime via `AI_ONCALL_TELEMETRY_STORE`. The base interface lives
in `base.py`; sqlite/duckdb/snowflake are sibling modules.
"""

from ai_oncall.storage.base import TelemetryStore

__all__ = ["TelemetryStore"]
