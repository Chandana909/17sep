PY ?= python

.PHONY: check lint type test fmt

check: lint type test

lint:
	$(PY) -m ruff check src tests scripts
	$(PY) -m ruff format --check src tests scripts

type:
	$(PY) -m mypy

test:
	$(PY) -m pytest

fmt:
	$(PY) -m ruff format src tests scripts
	$(PY) -m ruff check --fix src tests scripts
