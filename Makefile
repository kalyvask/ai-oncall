.PHONY: install test lint type contracts eval demo

install:
	pip install -e ".[dev]"
	pre-commit install

test:
	pytest

lint:
	ruff check ai_oncall tests
	ruff format --check ai_oncall tests

type:
	mypy --strict --ignore-missing-imports ai_oncall

contracts:
	pytest tests/contracts -q

eval:
	python -m evals.harness

demo:
	python scripts/demo.py
