PY ?= python
export PYTHONPATH := src

.PHONY: check fmt test demo serve

check:
	$(PY) scripts/check.py

fmt:
	$(PY) -m ruff format src tests scripts
	$(PY) -m ruff check --fix src tests scripts

test:
	$(PY) -m pytest

demo:
	$(PY) -m asas demo --db out/asas.db --report out/evaluation.md

serve:
	$(PY) -m asas serve --db out/asas.db
