# chipgraph developer tasks
#
# Targets:
#   sync    install dependencies (uv sync)
#   lint    ruff check + ruff format --check
#   fmt     ruff format + ruff check --fix
#   type    mypy
#   test    pytest
#   check   lint + type + test
#   schemas regenerate schemas/ from the pydantic contracts (M0-02)

.PHONY: sync lint fmt type test check schemas

sync:
	uv sync

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

type:
	uv run mypy

test:
	uv run pytest

check: lint type test

schemas:
	@if [ ! -d src/chipgraph/core/contracts ]; then \
		echo "schemas: added by task M0-02"; \
	else \
		uv run python -m chipgraph.core.contracts.export; \
	fi
