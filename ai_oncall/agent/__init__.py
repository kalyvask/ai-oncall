"""Stages 3-5 (PLAN, INVESTIGATE, SYNTHESIZE).

The redesigned tool-using loop lives here. Hard cap: 8 tool calls per incident
(BRIEF.md §4). The 6 tools live in `tools.py`; one prompt file per stage in
`prompts/`, versioned by filename suffix.
"""
